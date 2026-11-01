#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#5325 — disque des VM de runners de la CI : rapport, alerte et ménage sans root.

Constat (1/11/2026, mesure dev3 #5321 12:05Z) : `ameesh-ci-1` à 94 %
(2,3 GiB libres), `ameesh-ci-3` à 87 %, et `test_l159e_guard` échoue sous
2 GiB libres (le garde-fou de capacité publie `disk_mb` = 0) ; sous ~0, toute
la CI ameesh tombe. Seul le poste a root sur ces VM ; le ménage part donc des
jobs, avec le compte `runner` (membre du groupe docker).

Ce script ne supprime jamais ce qu'un job en cours utilise — chaque famille a
une borne d'âge supérieure au plafond d'un job (45 min) :

* conteneurs arrêtés créés il y a plus de 6 h (`docker container prune
  --filter until=`) ;
* volumes *anonymes* (nom de 64 hexadécimaux, aucun conteneur — arrêté ou
  non — ne les référence) créés il y a plus de 6 h ; l'âge vient de
  `docker volume inspect`, un volume dont la date est illisible est gardé ;
  un volume *nommé* n'est jamais supprimé : il peut porter des données ;
* images *dangling* (sans étiquette, donc sans conteneur) de plus de 24 h ;
* cache de construction inutilisé depuis plus de 24 h ;
* fichiers de plus de 48 h sous les `_work/_temp` des runners de l'hôte.

Rien n'est supprimé hors CI (`GITHUB_ACTIONS` absent) sans `--force` explicite.
Les erreurs de docker ne font jamais rougir l'étape : le ménage est un confort,
pas un verdict (canon §3) ; le rapport, lui, publie une annotation
`::warning::` sous le seuil (5 GiB par défaut, `AMEESH_CI_DISQUE_MIN_GIB`).

Le `postgres:17` des jobs est lancé avec `--tmpfs /var/lib/postgresql/data` :
le VOLUME de l'image n'est pas monté, l'hypothèse « volumes anonymes de
postgres » du lot ne tient donc pas ; `rapport --complet` dit où va le disque
(docker system df, `du` borné sous /home/runner) avant de toucher à quoi que ce
soit.

Usage :
  scripts/ci-disque.py rapport [--complet] [--seuil-gib N]
  scripts/ci-disque.py menage [--dry-run] [--force]
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

GIB = 1024 ** 3
SEUIL_GIB = 5.0
AGE_HEURES = 6.0            # conteneurs arrêtés et volumes non utilisés
AGE_IMAGES_HEURES = 24.0    # images dangling et cache de construction
AGE_TEMP_HEURES = 48.0      # fichiers abandonnés sous _work/_temp
VERROU = "/tmp/ameesh-ci-disque-%s.lock" % os.getuid()
MAX_VOLUMES_INSPECT = 100   # par appel docker (borne la ligne de commande)
MAX_LIGNES_DFV = 400        # `docker system df -v` : les 400 premières lignes


# --------------------------------------------------------------------------
# mesures et formatage
# --------------------------------------------------------------------------

def espace(chemin: str = "/") -> dict:
    """Espace du système de fichiers qui porte `chemin`, comme `df`."""
    st = os.statvfs(chemin)
    total = st.f_blocks * st.f_frsize
    libre = st.f_bavail * st.f_frsize
    reserve = st.f_bfree * st.f_frsize
    utilise = max(0, total - reserve)
    denominateur = utilise + libre
    return {"chemin": chemin, "total": total, "libre": libre,
            "utilise_pct": (100.0 * utilise / denominateur) if denominateur else 0.0}


def gib(octets: float | None) -> str:
    if octets is None:
        return "—"
    return "%.2f GiB" % (octets / GIB)


def seuil_depuis_env(environ=None, defaut: float = SEUIL_GIB) -> float:
    """Seuil d'alerte en GiB ; une valeur absente ou illisible prend le défaut."""
    environ = os.environ if environ is None else environ
    brut = environ.get("AMEESH_CI_DISQUE_MIN_GIB")
    if brut is None or not str(brut).strip():
        return defaut
    try:
        valeur = float(str(brut).strip().replace(",", "."))
    except ValueError:
        return defaut
    return valeur if valeur >= 0 else defaut


def _date(texte) -> float | None:
    """Horodatage RFC 3339 de docker → epoch ; illisible → None (on garde)."""
    if not texte:
        return None
    try:
        return dt.datetime.fromisoformat(str(texte).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def est_anonyme(nom: str) -> bool:
    """Un volume anonyme (créé par un conteneur sans nom) : 64 hexadécimaux.

    Les volumes *nommés* ne sont jamais supprimés par ce script : sur un hôte
    partagé, `ameesh-pg-1333-data` ou `memoria-db` peuvent être des données
    qu'un conteneur arrêté reprendra ; `docker volume prune` les enlèverait.
    """
    return bool(re.fullmatch(r"[0-9a-f]{64}", nom or ""))


def volumes_a_supprimer(inspecte: list[dict], maintenant: float,
                        age_heures: float) -> tuple[list[str], list[str]]:
    """(volumes anonymes assez vieux, volumes gardés) : sans date lisible, on garde."""
    vieux, gardes = [], []
    for volume in inspecte:
        nom = volume.get("Name")
        if not nom:
            continue
        cree = _date(volume.get("CreatedAt"))
        if (not est_anonyme(nom) or cree is None
                or maintenant - cree < age_heures * 3600.0):
            gardes.append(nom)
        else:
            vieux.append(nom)
    return vieux, gardes


def taille_docker(texte: str) -> int:
    """« Total reclaimed space: 1.234GB » de `docker … prune` → octets (SI)."""
    m = re.search(r"Total reclaimed space:\s*([0-9]+(?:\.[0-9]+)?)\s*([kKMGT]?B)",
                  texte or "")
    if not m:
        return 0
    unites = {"B": 1, "kB": 10 ** 3, "KB": 10 ** 3, "MB": 10 ** 6, "GB": 10 ** 9,
              "TB": 10 ** 12}
    return int(float(m.group(1)) * unites.get(m.group(2), 1))


# --------------------------------------------------------------------------
# exécution bornée
# --------------------------------------------------------------------------

def commande(argv: list[str], timeout: float = 120.0) -> tuple[int, str, str]:
    """(code, sortie, erreur) ; jamais d'exception : un outil absent vaut 127."""
    try:
        fini = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "", "%s introuvable" % argv[0]
    except subprocess.TimeoutExpired:
        return 124, "", "%s : délai de %s s dépassé" % (argv[0], timeout)
    except OSError as erreur:  # noqa: BLE001 — le ménage ne tombe jamais
        return 126, "", "%s : %s" % (argv[0], erreur)
    return fini.returncode, fini.stdout or "", fini.stderr or ""


def docker_pret() -> tuple[bool, str]:
    if not shutil.which("docker"):
        return False, "docker absent du PATH"
    code, sortie, erreur = commande(["docker", "info", "--format", "{{.ServerVersion}}"],
                                    timeout=20)
    if code != 0:
        return False, "docker info a échoué : %s" % (erreur.strip() or "code %d" % code)
    return True, sortie.strip()


# --------------------------------------------------------------------------
# rapport
# --------------------------------------------------------------------------

def _racine_runner() -> Path | None:
    """Le dossier du runner (…/r4) déduit de RUNNER_WORKSPACE (…/r4/_work/ameesh)."""
    espace_travail = os.environ.get("RUNNER_WORKSPACE")
    if not espace_travail:
        return None
    chemin = Path(espace_travail)
    if chemin.parent.name == "_work":
        return chemin.parent.parent
    return None


def _docker_df(complet: bool) -> list[str]:
    lignes: list[str] = []
    code, sortie, erreur = commande(["docker", "system", "df"], timeout=30)
    if code == 0:
        lignes = sortie.strip().splitlines()
    elif erreur.strip():
        lignes = [erreur.strip()]
    if complet:
        code, sortie, erreur = commande(["docker", "system", "df", "-v"], timeout=60)
        if code == 0:
            detail = sortie.strip().splitlines()
            if len(detail) > MAX_LIGNES_DFV:
                detail = detail[:MAX_LIGNES_DFV] + [
                    "… (%d lignes de plus, sortie tronquée)" % (len(detail) - MAX_LIGNES_DFV)]
            lignes += [""] + detail
        elif erreur.strip():
            lignes += ["", erreur.strip()]
    return lignes


def _comptes_docker() -> dict:
    def _lignes(argv):
        code, sortie, _ = commande(argv, timeout=20)
        return [ligne for ligne in sortie.splitlines() if ligne.strip()] if code == 0 else None

    return {
        "conteneurs": _lignes(["docker", "ps", "-aq"]),
        "conteneurs_actifs": _lignes(["docker", "ps", "-q"]),
        "images": _lignes(["docker", "images", "-q"]),
        "images_dangling": _lignes(["docker", "images", "-q", "--filter", "dangling=true"]),
        "volumes": _lignes(["docker", "volume", "ls", "-q"]),
        "volumes_dangling": _lignes(["docker", "volume", "ls", "-q", "--filter", "dangling=true"]),
    }


def _du(chemin: Path, timeout: float = 30.0) -> int | None:
    if not chemin.exists():
        return None
    code, sortie, _ = commande(["du", "-s", "-x", "-B1", str(chemin)], timeout=timeout)
    if code != 0 or not sortie.strip():
        return None
    try:
        return int(sortie.split()[0])
    except (ValueError, IndexError):
        return None


def _du_complet(racine: Path | None, espace_travail: str | None) -> list[tuple[str, int | None]]:
    """Les tailles utiles sous le home runner, bornées (une mesure, pas un scan)."""
    if racine is None:
        return []
    home = racine.parent
    cibles: list[tuple[str, Path]] = []
    if espace_travail:
        cibles.append(("checkout du job", Path(espace_travail)))
    cibles.append(("cache pip", home / ".cache" / "pip"))
    cibles.append(("home du runner", home))
    for _work_tool in sorted(home.glob("*/_work/_tool")):
        cibles.append(("outils %s" % _work_tool.parent.parent.name, _work_tool))
    cibles.append(("_work du runner", racine / "_work"))
    cibles.append(("_diag du runner", racine / "_diag"))
    cibles.append(("/tmp", Path("/tmp")))
    return [(libelle, _du(chemin)) for libelle, chemin in cibles]


def rapport(complet: bool = False, seuil_gib: float | None = None) -> dict:
    """Mesure, imprime le rapport et l'annotation ; renvoie l'état mesuré."""
    seuil = seuil_depuis_env() if seuil_gib is None else seuil_gib
    disque = espace("/")
    hote = socket.gethostname()
    runner = os.environ.get("RUNNER_NAME", "runner inconnu")
    etat = {"hote": hote, "runner": runner, "disque": disque, "seuil_gib": seuil,
            "docker": None, "comptes": None, "du": [], "df": [], "dfv": []}

    print("disque %s (%s) : %s libres sur %s (%.0f %% utilisés), seuil %.1f GiB"
          % (hote, runner, gib(disque["libre"]), gib(disque["total"]),
             disque["utilise_pct"], seuil))

    pret, version = docker_pret()
    if pret:
        etat["docker"] = version
        etat["comptes"] = _comptes_docker()
        comptes = etat["comptes"]
        def _n(cle):
            return "?" if comptes.get(cle) is None else str(len(comptes[cle]))
        print("docker %s : conteneurs %s (%s en cours), images %s (%s dangling), "
              "volumes %s (%s dangling)"
              % (version, _n("conteneurs"), _n("conteneurs_actifs"), _n("images"),
                 _n("images_dangling"), _n("volumes"), _n("volumes_dangling")))
        etat["df"] = _docker_df(complet)
        for ligne in etat["df"]:
            print(ligne)
        if complet:
            etat["du"] = _du_complet(_racine_runner(),
                                     os.environ.get("RUNNER_WORKSPACE"))
            for libelle, octets in etat["du"]:
                print("du %-24s %s" % (libelle, gib(octets)))
    else:
        print("docker indisponible : %s" % version)

    manque = disque["libre"] < seuil * GIB
    if manque:
        print("::warning::disque de %s (%s) : %s libres, sous le seuil de %.1f GiB "
              "(#5325)" % (hote, runner, gib(disque["libre"]), seuil))
    etat["alerte"] = manque
    _resume_rapport(etat)
    return etat


def _resume_rapport(etat: dict) -> None:
    lignes = ["### Disque de l'hôte %s (`%s`)" % (etat["hote"], etat["runner"]), "",
              "| | |", "|---|---|",
              "| libre sur `/` | **%s** / %s (%.0f %% utilisés) |"
              % (gib(etat["disque"]["libre"]), gib(etat["disque"]["total"]),
                 etat["disque"]["utilise_pct"]),
              "| seuil d'alerte | %.1f GiB |" % etat["seuil_gib"]]
    if etat["comptes"] is not None:
        comptes = etat["comptes"]
        def _n(cle):
            return "?" if comptes.get(cle) is None else str(len(comptes[cle]))
        lignes.append("| docker | conteneurs %s (%s en cours), images %s "
                      "(%s dangling), volumes %s (%s dangling) |"
                      % (_n("conteneurs"), _n("conteneurs_actifs"), _n("images"),
                         _n("images_dangling"), _n("volumes"), _n("volumes_dangling")))
    if etat["du"]:
        lignes += ["", "| chemin | taille |", "|---|---|"]
        lignes += ["| %s | %s |" % (libelle, gib(octets)) for libelle, octets in etat["du"]]
    if etat["df"]:
        lignes += ["", "```", *etat["df"], "```"]
    if etat["alerte"]:
        lignes += ["", "⚠ disque sous le seuil : le ménage des jobs (#5325) et une "
                   "intervention du poste sont nécessaires."]
    _resume("\n".join(lignes) + "\n")


def _resume(texte: str) -> None:
    """Ajoute au résumé du job quand GitHub le demande."""
    chemin = os.environ.get("GITHUB_STEP_SUMMARY")
    if not chemin:
        return
    try:
        with open(chemin, "a", encoding="utf-8") as fichier:
            fichier.write(texte)
    except OSError:
        pass


# --------------------------------------------------------------------------
# ménage
# --------------------------------------------------------------------------

def _verrou():
    """Verrou par hôte : huit runners partagent le démon docker ; un seul élague."""
    try:
        fichier = open(VERROU, "a+", encoding="utf-8")
    except OSError as erreur:
        print("ménage : verrou illisible (%s), rien n'est supprimé" % erreur)
        return None
    try:
        fcntl.flock(fichier.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fichier.close()
        print("ménage : un autre job élague déjà cet hôte, rien n'est supprimé")
        return None
    return fichier


def _conteneurs(dry_run: bool) -> dict:
    argv = ["docker", "container", "prune", "-f", "--filter", "until=%.0fh" % AGE_HEURES]
    if dry_run:
        print("+ " + " ".join(argv))
        return {"etape": "conteneurs arrêtés > 6 h", "libere": 0, "sortie": ""}
    code, sortie, erreur = commande(argv, timeout=180)
    return {"etape": "conteneurs arrêtés > 6 h", "libere": taille_docker(sortie),
            "sortie": (sortie + erreur).strip(), "code": code}


def _volumes(dry_run: bool) -> dict:
    etape = {"etape": "volumes anonymes inutilisés > 6 h", "libere": 0, "sortie": ""}
    code, sortie, erreur = commande(
        ["docker", "volume", "ls", "-q", "--filter", "dangling=true"], timeout=30)
    if code != 0:
        etape["sortie"] = "docker volume ls a échoué : %s" % (erreur.strip() or code)
        return etape
    noms = [nom for nom in sortie.split() if nom]
    if not noms:
        etape["sortie"] = "aucun volume dangling"
        return etape
    inspecte: list[dict] = []
    for debut in range(0, len(noms), MAX_VOLUMES_INSPECT):
        bloc = noms[debut:debut + MAX_VOLUMES_INSPECT]
        code, sortie, erreur = commande(["docker", "volume", "inspect", *bloc], timeout=60)
        try:
            inspecte += json.loads(sortie) if sortie.strip() else []
        except ValueError:
            etape["sortie"] = "docker volume inspect illisible : %s" % (erreur.strip() or sortie[:200])
            return etape
    vieux, gardes = volumes_a_supprimer(inspecte, dt.datetime.now(dt.timezone.utc).timestamp(),
                                        AGE_HEURES)
    if gardes:
        print("ménage : %d volume(s) gardé(s) (nommé(s), créés il y a moins de %.0f h "
              "ou date illisible)" % (len(gardes), AGE_HEURES))
    if not vieux:
        etape["sortie"] = "aucun volume assez vieux"
        return etape
    if dry_run:
        print("+ docker volume rm " + " ".join(vieux))
        etape["sortie"] = "%d volume(s) seraient supprimé(s)" % len(vieux)
        return etape
    codes = []
    for debut in range(0, len(vieux), MAX_VOLUMES_INSPECT):
        bloc = vieux[debut:debut + MAX_VOLUMES_INSPECT]
        code, sortie, erreur = commande(["docker", "volume", "rm", *bloc], timeout=120)
        codes.append((code, (sortie + erreur).strip()))
    etape["sortie"] = "; ".join(texte for _, texte in codes if texte)[:2000]
    etape["enleves"] = sum(1 for code, _ in codes if code == 0)
    return etape


def _images(dry_run: bool) -> dict:
    argv = ["docker", "image", "prune", "-f", "--filter",
            "until=%.0fh" % AGE_IMAGES_HEURES]
    if dry_run:
        print("+ " + " ".join(argv))
        return {"etape": "images dangling > 24 h", "libere": 0, "sortie": ""}
    code, sortie, erreur = commande(argv, timeout=180)
    return {"etape": "images dangling > 24 h", "libere": taille_docker(sortie),
            "sortie": (sortie + erreur).strip(), "code": code}


def _cache_build(dry_run: bool) -> dict:
    argv = ["docker", "builder", "prune", "-f", "--filter",
            "until=%.0fh" % AGE_IMAGES_HEURES]
    if dry_run:
        print("+ " + " ".join(argv))
        return {"etape": "cache de construction > 24 h", "libere": 0, "sortie": ""}
    code, sortie, erreur = commande(argv, timeout=180)
    if code != 0 and "unknown command" in (erreur + sortie).lower():
        return {"etape": "cache de construction > 24 h", "libere": 0,
                "sortie": "docker builder absent (ancien démon), étape sautée"}
    return {"etape": "cache de construction > 24 h", "libere": taille_docker(sortie),
            "sortie": (sortie + erreur).strip(), "code": code}


def _temp(dry_run: bool) -> dict:
    """Fichiers abandonnés (> 48 h) des _temp de tous les runners de l'hôte."""
    racine = _racine_runner()
    if racine is None:
        return {"etape": "_temp des runners > 48 h", "libere": 0,
                "sortie": "RUNNER_WORKSPACE absent, étape sautée"}
    total = 0
    trouve = 0
    for dossier in sorted(racine.parent.glob("*/_work/_temp")):
        if not dossier.is_dir():
            continue
        code, sortie, erreur = commande(
            ["find", str(dossier), "-mindepth", "1", "-type", "f",
             "-mtime", "+%d" % int(AGE_TEMP_HEURES // 24)], timeout=60)
        if code != 0:
            continue
        fichiers = [ligne for ligne in sortie.splitlines() if ligne]
        if not fichiers:
            continue
        octets = 0
        for fichier in fichiers:
            try:
                octets += os.lstat(fichier).st_size
            except OSError:
                pass
        trouve += len(fichiers)
        total += octets
        if dry_run:
            print("+ find %s -type f -mtime +%d -delete  (%d fichier(s))"
                  % (dossier, int(AGE_TEMP_HEURES // 24), len(fichiers)))
        else:
            commande(["find", str(dossier), "-mindepth", "1", "-type", "f",
                      "-mtime", "+%d" % int(AGE_TEMP_HEURES // 24), "-delete"], timeout=60)
    return {"etape": "_temp des runners > 48 h", "libere": 0 if dry_run else total,
            "sortie": "%d fichier(s) %s" % (trouve, "repéré(s)" if dry_run else "supprimé(s)")}


def menage(dry_run: bool = False, force: bool = False) -> int:
    if os.environ.get("GITHUB_ACTIONS") != "true" and not force:
        print("ménage : hors CI (GITHUB_ACTIONS != true), rien n'est supprimé ; "
              "--force pour un essai explicite")
        return 0
    pret, version = docker_pret()
    if not pret:
        print("ménage : docker indisponible (%s), rien n'est supprimé" % version)
        return 0
    verrou = _verrou()
    if verrou is None:
        return 0
    try:
        avant = espace("/")
        print("ménage de %s (%s) : %s libres avant (docker %s)"
              % (socket.gethostname(), os.environ.get("RUNNER_NAME", "runner inconnu"),
                 gib(avant["libre"]), version))
        resultats = []
        for etape in (_conteneurs, _volumes, _images, _cache_build, _temp):
            try:
                resultat = etape(dry_run)
            except Exception as erreur:  # noqa: BLE001 — jamais un verdict
                resultat = {"etape": etape.__name__, "libere": 0,
                            "sortie": "échec inattendu : %s" % erreur}
            resultats.append(resultat)
            print("ménage %-30s %s %s" % (resultat["etape"], gib(resultat["libere"]),
                                          ("— " + resultat["sortie"]) if resultat["sortie"] else ""))
        apres = espace("/") if not dry_run else avant
        if not dry_run:
            libre = max(0, apres["libre"] - avant["libre"])
            print("ménage : %s libres après (%s libérés ; les volumes et images "
                  "comptent à leur taille docker, le disque peut bouger avec les jobs "
                  "en cours)" % (gib(apres["libre"]), gib(libre)))
        _resume_menage(avant, apres, resultats, dry_run)
        return 0
    finally:
        verrou.close()


def _resume_menage(avant: dict, apres: dict, resultats: list[dict], dry_run: bool) -> None:
    lignes = ["### Ménage du disque — %s (`%s`)%s"
              % (socket.gethostname(), os.environ.get("RUNNER_NAME", "runner inconnu"),
                 " (essai, rien supprimé)" if dry_run else ""), "",
              "| étape | libéré | détail |", "|---|---|---|"]
    for resultat in resultats:
        detail = (resultat.get("sortie") or "").replace("|", "\\|").replace("\n", " ")
        lignes.append("| %s | %s | %s |"
                      % (resultat["etape"], gib(resultat["libere"]), detail[:300]))
    lignes += ["", "libre avant : **%s**, après : **%s**."
               % (gib(avant["libre"]), gib(apres["libre"]))]
    _resume("\n".join(lignes) + "\n")


# --------------------------------------------------------------------------
# ligne de commande
# --------------------------------------------------------------------------

def _analyse(argv=None):
    analyseur = argparse.ArgumentParser(
        description="Disque des VM de runners de la CI (#5325) : rapport, alerte, ménage.")
    sous = analyseur.add_subparsers(dest="mode", required=True)
    rapport_parser = sous.add_parser("rapport", help="mesure, alerte et résumé du job")
    rapport_parser.add_argument("--complet", action="store_true",
                                help="docker system df -v et du borné sous /home/runner")
    rapport_parser.add_argument("--seuil-gib", type=float, default=None,
                                help="seuil d'alerte en GiB (défaut %(default)s)")
    menage_parser = sous.add_parser("menage", help="élague ce qui n'est plus utilisé")
    menage_parser.add_argument("--dry-run", action="store_true",
                               help="imprime les commandes, ne supprime rien")
    menage_parser.add_argument("--force", action="store_true",
                               help="accepte de tourner hors CI (essai explicite)")
    return analyseur.parse_args(argv)


def main(argv=None) -> int:
    args = _analyse(argv)
    if args.mode == "rapport":
        rapport(complet=args.complet, seuil_gib=args.seuil_gib)
        return 0
    return menage(dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
