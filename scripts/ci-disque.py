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
* avec `--images-inutilisees` seulement (ménage dédié, jamais dans un job
  de tests) : images sans conteneur créées il y a plus de 7 jours ; les
  images de construction (`rust`, `node`, `ubuntu`) dorment des semaines sur
  les VM ; `prune` ne touche jamais une image qu'un conteneur référence — les
  jobs de tests ne enlèvent donc que les images *dangling*, pour ne pas
  re-télécharger `postgres:17` entre deux parts ;
* cache de construction inutilisé depuis plus de 24 h ;
* fichiers de plus de 48 h (au **ctime**, non antidatable sans root) sous les
  `_work/_temp` des runners de l'hôte et sous `/tmp` (compte `runner`
  seulement) ;
* journaux `_diag` fermés compressés après 72 h, archives retirées après
  7 jours supplémentaires (au ctime) ;
* caches `_work/_update` d'un auto-update déjà installé (ou dépassé), et
  seulement si aucun Listener, Worker, updater ou lien vivant ne les
  référence ; une version de cache supérieure à la version courante est une
  mise à jour en attente, jamais retirée (#5635).

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
  scripts/ci-disque.py mesure --host <label-machine>
  scripts/ci-disque.py rapport [--complet] [--seuil-gib N]
  scripts/ci-disque.py menage [--dry-run] [--force] [--images-inutilisees]
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

GIB = 1024 ** 3
SEUIL_GIB = 5.0
AGE_HEURES = 6.0            # conteneurs arrêtés et volumes non utilisés
AGE_IMAGES_HEURES = 24.0    # images dangling et cache de construction
AGE_TEMP_HEURES = 48.0      # fichiers abandonnés sous _work/_temp et /tmp
AGE_DIAG_JOURS = 7          # journaux du runner (_diag)
AGE_INUTILISEES_HEURES = 168.0  # images sans conteneur (7 jours)
MAX_VOLUMES_INSPECT = 100   # par appel docker (borne la ligne de commande)
MAX_LIGNES_DFV = 400        # `docker system df -v` : les 400 premières lignes
MAX_FICHIERS_DIAG = 128     # par passe : les fichiers _diag compressés ou retirés
BUDGET_COMPRESSION_S = 120.0  # par passe : au-delà, la compression en cours est reprise
BLOC_COMPRESSION = 8 * 1024 * 1024  # un membre gzip complet par bloc (reprise sûre)

# #5635 — caches _update des runners. Une extraction de paquet (~700 MiB) reste
# sous `_work/_update` après chaque auto-update ; le runner la supprime au début
# du suivant, pas à la fin du courant. On ne la retire que si la version qu'elle
# porte est déjà installée (ou dépassée) et qu'aucun processus vivant ne la
# référence. Une version supérieure est une mise à jour en attente : préservée.
DELAI_REDEMARRAGE_S = 600.0
MAX_ENTREES_UPDATE = 200000
INTERPRETEURS_SHELL = frozenset({"bash", "dash", "sh", "zsh", "ksh"})
VERSION_UPDATE = re.compile(r'"Runner\.Listener/(\d+\.\d+\.\d+)"')
ENTREES_UPDATE = frozenset({
    "bin", "externals", "config.sh", "env.sh", "config.cmd", "run.cmd",
    "run.sh", "run-helper.sh.template", "run-helper.cmd.template",
    "safe_sleep.sh",
})
ARCHIVE_UPDATE = re.compile(r"runner\d+\.(?:tar\.gz|zip)\Z")


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


def _du_complet(racine: Path | None) -> list[tuple[str, int | None]]:
    """Les tailles utiles sous le home runner, en UN `du` borné.

    Huit runners vivent sur l'hôte, chacun avec son checkout, son cache
    d'outils et ses journaux : le diagnostic doit voir les huit, pas seulement
    celui du job, sinon le compte ne tombe pas juste (mesure #5325 du 1/11).
    """
    if racine is None:
        return []
    home = racine.parent
    cibles: list[tuple[str, Path]] = [("cache pip", home / ".cache" / "pip")]
    for outil in sorted(home.glob("*/_work/_tool")):
        cibles.append(("outils %s" % outil.parent.parent.name, outil))
    for runner in sorted(p for p in home.glob("*") if p.is_dir() and p.name[:1] == "r"):
        if not runner.name[1:].isdigit():
            continue
        cibles.append(("_work %s" % runner.name, runner / "_work"))
        cibles.append(("cache _update %s" % runner.name,
                       runner / "_work" / "_update"))
        cibles.append(("_diag %s" % runner.name, runner / "_diag"))
    cibles.append(("/tmp", Path("/tmp")))
    argv = ["du", "-s", "-x", "-B1", *[str(chemin) for _, chemin in cibles]]
    code, sortie, _ = commande(argv, timeout=240)
    if code not in (0, 1) or not sortie.strip():
        return [(libelle, None) for libelle, _ in cibles]
    tailles: dict[str, int] = {}
    for ligne in sortie.splitlines():
        morceaux = ligne.split("\t", 1)
        if len(morceaux) != 2:
            continue
        try:
            tailles[morceaux[1].rstrip("/")] = int(morceaux[0])
        except ValueError:
            continue
    return [(libelle, tailles.get(str(chemin).rstrip("/"))) for libelle, chemin in cibles]


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
            etat["du"] = _du_complet(_racine_runner())
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


def mesure(hote=None):
    """#5630 : petite sonde indépendante de docker, lisible via Checks API."""
    disque = espace("/")
    document = {"schema": "ameesh-ci-disk/1", "host": hote or socket.gethostname(),
                "volume": disque["chemin"], "free_bytes": disque["libre"],
                "total_bytes": disque["total"], "used_pct": disque["utilise_pct"],
                "hostname": socket.gethostname()}
    # Une notice même au vert : sa lecture permet de résoudre l'alerte.
    print("::notice title=ameesh-ci-disk/1::" + json.dumps(document, separators=(",", ":")))
    return document


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

def _dossier_verrou() -> Path | None:
    """Le home du runner, jamais un arbre que ce script supprime.

    Le verrou a vécu dans /tmp (#5325, avis ameesh-cx1 B/P2) : au bout de
    trois jours, `_tmp` le supprimait pendant qu'un job le tenait, et un
    second job créait un autre inode puis verrouillait en parallèle. Le home
    du runner n'est touché par aucune étape du ménage.
    """
    racine = _racine_runner()
    if racine is not None:
        return racine.parent
    try:
        home = Path(os.path.expanduser("~"))
    except (KeyError, RuntimeError):
        return None
    return home if str(home) not in ("", "/") else None


def _verrou():
    """Verrou par hôte : huit runners partagent le démon docker ; un seul élague."""
    dossier = _dossier_verrou()
    if dossier is None:
        print("ménage : aucun dossier persistant pour le verrou, rien n'est supprimé")
        return None
    try:
        fichier = open(dossier / (".ameesh-ci-disque-%s.lock" % os.getuid()),
                       "a+", encoding="utf-8")
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
            etape["sortie"] = ("docker volume inspect illisible : %s"
                               % (erreur.strip() or sortie[:200]))
            return etape
    maintenant = dt.datetime.now(dt.timezone.utc).timestamp()
    vieux, gardes = volumes_a_supprimer(inspecte, maintenant, AGE_HEURES)
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


def _images_inutilisees(dry_run: bool) -> dict:
    """Images sans conteneur, créées il y a plus de 7 jours.

    Les images de construction (`rust`, `node`, `ubuntu`) dorment des semaines
    sur les VM ; une image supprimée est retirée par le prochain job qui en a
    besoin (`docker create` la télécharge), alors qu'un disque plein fait
    échouer ce téléchargement. Aucune image utilisée par un conteneur, arrêté
    ou non, n'est enlevée par `prune`.
    """
    argv = ["docker", "image", "prune", "-a", "-f", "--filter",
            "until=%.0fh" % AGE_INUTILISEES_HEURES]
    if dry_run:
        print("+ " + " ".join(argv))
        return {"etape": "images inutilisées > 7 j", "libere": 0, "sortie": ""}
    code, sortie, erreur = commande(argv, timeout=240)
    return {"etape": "images inutilisées > 7 j", "libere": taille_docker(sortie),
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


def _fichiers_anciens(dossiers, jours: int, dry_run: bool, user: int | None = None) -> dict:
    """Fichiers d'un dossier plus vieux que `jours` jours : mesurés, puis effacés.

    L'âge est le **ctime**, pas le mtime : un créateur antidate un mtime quand
    il veut (la suite ameesh le fait sous `TMPDIR=/tmp`, `os.utime(…, (100,
    100))` dans test_1190 et les lots 73/219/3277), et un autre job de la même
    VM supprimerait alors un fichier en cours de test (#5325, avis auditeur
    B/P2). Le ctime ne se change pas sans root.
    """
    total = trouve = 0
    for dossier in dossiers:
        if not dossier.is_dir():
            continue
        commun = [str(dossier), "-xdev", "-mindepth", "1"]
        if user is not None:
            commun += ["-user", str(user)]
        code, sortie, _ = commande(["find", *commun, "-ctime", "+%d" % jours,
                                    "-print"], timeout=120)
        if code != 0:
            continue
        liste = [ligne for ligne in sortie.splitlines() if ligne.strip()]
        octets = 0
        for chemin in liste:
            try:
                octets += os.lstat(chemin).st_size
            except OSError:
                pass
        fichiers = len(liste)
        if not fichiers:
            continue
        total += octets
        trouve += fichiers
        if dry_run:
            print("+ find %s -ctime +%d -delete  (%d fichier(s))" % (dossier, jours, fichiers))
        else:
            commande(["find", *commun, "-ctime", "+%d" % jours, "-delete"], timeout=120)
    return {"fichiers": trouve, "octets": 0 if dry_run else total}


def _temp(dry_run: bool) -> dict:
    """Fichiers abandonnés (> 48 h) des _temp de tous les runners de l'hôte."""
    racine = _racine_runner()
    if racine is None:
        return {"etape": "_temp des runners > 48 h", "libere": 0,
                "sortie": "RUNNER_WORKSPACE absent, étape sautée"}
    resultat = _fichiers_anciens(sorted(racine.parent.glob("*/_work/_temp")),
                                 int(AGE_TEMP_HEURES // 24), dry_run)
    return {"etape": "_temp des runners > 48 h", "libere": resultat["octets"],
            "sortie": "%d fichier(s)" % resultat["fichiers"]}


def _journaux_ouverts(proc=Path("/proc")) -> set:
    """Inodes ouverts par les runners ; lecture refusée = rotation abandonnée.

    Un Listener peut garder son journal ouvert plusieurs jours. Aucun contenu
    ni environnement de processus n'est lu, seulement les descripteurs.
    """
    ouverts = set()
    for processus in proc.iterdir():
        if not processus.name.isdigit():
            continue
        try:
            if processus.stat().st_uid != os.getuid():
                continue
            if (processus / "comm").read_text().strip() not in (
                    "Runner.Listener", "Runner.Worker"):
                continue
            for fd in (processus / "fd").iterdir():
                try:
                    info = fd.stat()
                    ouverts.add((info.st_dev, info.st_ino))
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass
    return ouverts


ETAPE_DIAG = "_diag fermés >72 h compressés, archives >7 j retirées"


def _fichier_partiel(chemin: Path) -> Path:
    return chemin.with_name(chemin.name + ".gz.part")


def _fichier_etat(chemin: Path) -> Path:
    return chemin.with_name(chemin.name + ".gz.state")


def _identite(info) -> dict:
    """Ce qui identifie le contenu d'un fichier sans le lire (ctime compris)."""
    return {cle: getattr(info, cle) for cle in
            ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")}


def _ecrire_etat(chemin: Path, etat: dict) -> None:
    """Écrit l'état atomiquement : un kill laisse l'ancien état, jamais un mixte."""
    temporaire = chemin.with_name(chemin.name + ".tmp")
    try:
        temporaire.write_text(json.dumps(etat), encoding="utf-8")
        os.replace(temporaire, chemin)
    except OSError:
        temporaire.unlink(missing_ok=True)
        raise


def _lire_etat(chemin: Path, identite: dict) -> dict | None:
    """L'état de reprise s'il décrit encore cette source, sinon None (résidu)."""
    try:
        etat = json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(etat, dict) or etat.get("identite") != identite
            or type(etat.get("offset")) is not int or etat["offset"] < 0
            or type(etat.get("part_size")) is not int or etat["part_size"] < 0):
        return None
    return etat


def _compresser_partiel(chemin: Path, initial, *, debut: float, budget_s: float,
                        taille_bloc: int) -> dict:
    """Compresse par membres gzip complets, repris à la passe suivante.

    Un membre gzip complet et un état écrit après chaque bloc : une passe qui
    atteint le budget laisse une archive partielle valide et son offset, la
    suivante continue au lieu de repartir de zéro. Un `.gz.part` sans état
    lisible, ou d'une autre source, est jeté puis recompressé : il ne bloque
    plus la rotation. Une source modifiée ou une archive déjà présente garde
    le journal original et enlève le partiel.
    """
    partiel = _fichier_partiel(chemin)
    etat_chemin = _fichier_etat(chemin)
    identite = _identite(initial)
    offset = 0
    residu = False
    if partiel.exists():
        etat = _lire_etat(etat_chemin, identite)
        if etat is None:
            partiel.unlink(missing_ok=True)
            etat_chemin.unlink(missing_ok=True)
            residu = True
        else:
            offset = etat["offset"]
            taille = partiel.stat().st_size
            if taille < etat["part_size"]:
                # Le partiel a été tronqué hors de notre contrôle : l'état ne
                # décrit plus son contenu, on repart de zéro.
                partiel.unlink(missing_ok=True)
                etat_chemin.unlink(missing_ok=True)
                offset, residu = 0, True
            elif taille > etat["part_size"]:
                # Processus tué entre l'écriture d'un membre complet et celle
                # de l'état : le préfixe enregistré est fsynqué et valide, les
                # octets au-delà ne sont pas référencés, on les coupe.
                with partiel.open("r+b") as fichier:
                    fichier.truncate(etat["part_size"])
                    fichier.flush()
                    os.fsync(fichier.fileno())
    if offset > initial.st_size:
        partiel.unlink(missing_ok=True)
        etat_chemin.unlink(missing_ok=True)
        offset, residu = 0, True

    try:
        with os.fdopen(os.open(chemin, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
            if _identite(os.fstat(source.fileno())) != identite:
                partiel.unlink(missing_ok=True)
                etat_chemin.unlink(missing_ok=True)
                return {"termine": False, "partiel": False, "octets": 0,
                        "residu": residu, "change": True}
            source.seek(offset)
            if initial.st_size == 0 and not partiel.exists():
                # Un journal vide reste un journal : une archive gzip vide est
                # valide et ne doit pas buter sur un partiel inexistant.
                descripteur = os.open(partiel, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                      0o600)
                with os.fdopen(descripteur, "wb") as sortie:
                    with gzip.GzipFile(filename="", mode="wb", fileobj=sortie):
                        pass
                    sortie.flush()
                    os.fsync(sortie.fileno())
            while bloc := source.read(taille_bloc):
                descripteur = os.open(partiel, os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                                      0o600)
                with os.fdopen(descripteur, "wb") as sortie:
                    with gzip.GzipFile(filename="", mode="wb", fileobj=sortie) as gz:
                        gz.write(bloc)
                    sortie.flush()
                    os.fsync(sortie.fileno())
                offset += len(bloc)
                _ecrire_etat(etat_chemin, {"identite": identite, "offset": offset,
                                           "part_size": partiel.stat().st_size})
                if time.monotonic() - debut >= budget_s:
                    return {"termine": False, "partiel": True, "octets": 0,
                            "residu": residu}
            final = chemin.lstat()
            if _identite(final) != identite:
                partiel.unlink(missing_ok=True)
                etat_chemin.unlink(missing_ok=True)
                return {"termine": False, "partiel": False, "octets": 0,
                        "residu": residu, "change": True}
            archive = chemin.with_name(chemin.name + ".gz")
            try:
                os.link(partiel, archive)  # refuse une archive déjà présente
            except FileExistsError:
                partiel.unlink(missing_ok=True)
                etat_chemin.unlink(missing_ok=True)
                return {"termine": False, "partiel": False, "octets": 0,
                        "residu": residu, "archive": True}
            chemin.unlink()
            partiel.unlink(missing_ok=True)
            etat_chemin.unlink(missing_ok=True)
            return {"termine": True, "partiel": False,
                    "octets": max(0, initial.st_size - archive.stat().st_size),
                    "residu": residu}
    except OSError:
        # Échec réel (disque plein, droit) : on jette le partiel pour ne pas
        # tromper une reprise, et la source reste intacte.
        partiel.unlink(missing_ok=True)
        etat_chemin.unlink(missing_ok=True)
        raise


def _rotation_diag(dossiers, dry_run=False, *, maintenant=None,
                   budget_s: float = BUDGET_COMPRESSION_S,
                   taille_bloc: int = BLOC_COMPRESSION, ouverts=None) -> dict:
    """Logs fermés >72 h compressés ; archives gardées encore 7 jours.

    Ctime, pas mtime antidatable. Ne suit aucun lien, n'écrase aucune archive.
    Budget 128 fichiers / 120 secondes par passe : le budget ne coupe
    qu'entre deux fichiers ou dans une compression, qui laisse alors son état
    pour la passe suivante (une passe fait toujours progresser un gros log).
    """
    debut = time.monotonic()
    maintenant = time.time() if maintenant is None else maintenant
    if ouverts is None:
        ouverts = _journaux_ouverts()
    compte = libere = partiels = residus = traites = 0
    interrompu = False
    chemins = [p for dossier in dossiers for p in sorted(dossier.glob("*.log*"))
               if not p.name.endswith((".gz.part", ".gz.state", ".gz.state.tmp"))]
    for chemin in chemins:
        # `traites` : au moins une tentative par passe, même si le budget est
        # déjà dépensé, pour que le gros log en cours avance toujours.
        if compte >= MAX_FICHIERS_DIAG or (traites and
                                           time.monotonic() - debut >= budget_s):
            interrompu = True
            break
        initial = chemin.lstat()
        if (not chemin.name.startswith(("Runner_", "Worker_"))
                or not stat.S_ISREG(initial.st_mode) or initial.st_uid != os.getuid()
                or (initial.st_dev, initial.st_ino) in ouverts):
            continue
        age = maintenant - initial.st_ctime
        if chemin.name.endswith(".log.gz"):
            if age > AGE_DIAG_JOURS * 86400:
                if not dry_run:
                    chemin.unlink()
                    libere += initial.st_size
                compte += 1
                traites += 1
            continue
        if chemin.suffix != ".log" or age <= 72 * 3600:
            continue
        if dry_run:
            print("+ gzip " + str(chemin))
            compte += 1
            traites += 1
            continue
        resultat = _compresser_partiel(chemin, initial, debut=debut,
                                       budget_s=budget_s, taille_bloc=taille_bloc)
        traites += 1
        residus += 1 if resultat.get("residu") else 0
        if resultat["termine"]:
            compte += 1
            libere += resultat["octets"]
        elif resultat["partiel"]:
            partiels += 1
            interrompu = True
            break
    return {"fichiers": compte, "octets": libere, "partiels": partiels,
            "residus": residus, "interrompu": interrompu}


def _diag(dry_run: bool) -> dict:
    """Rotation des journaux sans toucher aux caches/updaters des runners."""
    racine = _racine_runner()
    if racine is None:
        return {"etape": "_diag des runners > 7 j", "libere": 0,
                "sortie": "RUNNER_WORKSPACE absent, étape sautée"}
    dossiers = [p for p in sorted(racine.parent.glob("*/_diag"))
                if not p.is_symlink() and not p.parent.is_symlink()]
    try:
        ouverts = _journaux_ouverts()
    except PermissionError as erreur:
        # #5633 : le refus est dit dans le rapport, la rotation n'est pas tentée.
        return {"etape": ETAPE_DIAG, "libere": 0,
                "sortie": "descripteurs des runners illisibles (PermissionError : %s) ; "
                          "rotation non tentée" % erreur}
    try:
        resultat = _rotation_diag(dossiers, dry_run, ouverts=ouverts)
    except PermissionError as erreur:
        return {"etape": ETAPE_DIAG, "libere": 0,
                "sortie": "lecture d'un journal refusée (PermissionError : %s) ; "
                          "rotation interrompue" % erreur}
    detail = "%d fichier(s)" % resultat["fichiers"]
    if resultat["residus"]:
        detail += ", %d résidu(s) .gz.part ignoré(s)" % resultat["residus"]
    if resultat["partiels"]:
        detail += ", %d compression(s) en cours" % resultat["partiels"]
    if resultat["interrompu"]:
        detail += " (budget atteint, reprise à la passe suivante)"
    return {"etape": ETAPE_DIAG, "libere": resultat["octets"], "sortie": detail}


def _tmp(dry_run: bool) -> dict:
    """Fichiers du runner abandonnés dans /tmp depuis plus de 48 h.

    `-user` borne à notre compte : les fichiers de root ou d'un autre service
    restent. Un job dure 45 min au plus ; deux jours est une marge large, et
    le nom disparu ne casse rien (l'inode reste ouvert s'il l'est encore).
    """
    resultat = _fichiers_anciens([Path("/tmp")], int(AGE_TEMP_HEURES // 24),
                                 dry_run, user=os.getuid())
    return {"etape": "/tmp du runner > 48 h", "libere": resultat["octets"],
            "sortie": "%d fichier(s)" % resultat["fichiers"]}


# --------------------------------------------------------------------------
# caches _update des runners (#5635)
# --------------------------------------------------------------------------

def _version_tuple(texte):
    """« 2.338.2 » → (2, 338, 2) ; illisible → None (on ne compare pas)."""
    try:
        parties = tuple(int(partie) for partie in str(texte).split("."))
    except (TypeError, ValueError):
        return None
    return parties if parties else None


def _lire_version_bin(dossier: Path) -> str | None:
    """Version du paquet dans un `bin`, sans exécuter le binaire.

    Le fichier `Runner.Listener.deps.json` du paquet officiel porte
    `"Runner.Listener/2.338.2"` : une lecture suffit, aucun `--version`.
    """
    chemin = dossier / "Runner.Listener.deps.json"
    try:
        if chemin.is_symlink() or not chemin.is_file():
            return None
        with open(chemin, "rb") as fichier:
            donnees = fichier.read(1_000_000)
    except OSError:
        return None
    correspondance = VERSION_UPDATE.search(donnees.decode("utf-8", "replace"))
    return correspondance.group(1) if correspondance else None


def _lire_version(dossier: Path) -> str | None:
    return _lire_version_bin(dossier / "bin")


def _empreinte(chemin: Path) -> bytes | None:
    try:
        if chemin.is_symlink() or not chemin.is_file():
            return None
        return hashlib.sha256(chemin.read_bytes()).digest()
    except OSError:
        return None


def _sous(chemin, parent) -> bool:
    """Vrai si `chemin` est `parent` ou l'un de ses descendants (lexical)."""
    if chemin is None or parent is None:
        return False
    try:
        chemin = Path(chemin)
        parent = Path(parent)
    except TypeError:
        return False
    return chemin == parent or parent in chemin.parents


def _processus(proc: Path = Path("/proc"), *, maintenant=None) -> list[dict]:
    """Instantané des processus du même uid : cwd, descripteurs, cmdline, départ.

    Aucun contenu mémoire ni environnement n'est lu (#5635 : les règles du
    canon interdisent `/proc/<pid>/environ`). Un champ illisible reste None ;
    l'appelant traite ce doute comme une raison de ne pas supprimer.
    """
    maintenant = time.time() if maintenant is None else maintenant
    try:
        uptime = float((proc / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        uptime = None
    horloge = os.sysconf("SC_CLK_TCK")
    vus = []
    for entree in proc.iterdir():
        if not entree.name.isdigit():
            continue
        try:
            if entree.stat().st_uid != os.getuid():
                continue
            comm = (entree / "comm").read_text().strip()
        except OSError:
            continue
        try:
            cwd = Path(os.readlink(entree / "cwd"))
        except OSError:
            cwd = None
        try:
            descripteurs = []
            for fd in (entree / "fd").iterdir():
                try:
                    cible = os.readlink(fd)
                except OSError:
                    continue
                if cible.endswith(" (deleted)"):
                    cible = cible[:-10]
                descripteurs.append(Path(cible))
        except OSError:
            descripteurs = None
        try:
            brut = (entree / "cmdline").read_bytes()
            cmdline = [morceau.decode("utf-8", "replace")
                       for morceau in brut.split(b"\0") if morceau]
        except OSError:
            cmdline = None
        etat = None
        depart = None
        try:
            statut = (entree / "stat").read_text()
            champs = statut.rsplit(")", 1)[1].split()
            etat = champs[0]
            if uptime is not None:
                depart = maintenant - (uptime - int(champs[19]) / horloge)
        except (OSError, ValueError, IndexError):
            pass
        vus.append({"pid": int(entree.name), "comm": comm, "cwd": cwd,
                    "fds": descripteurs, "cmdline": cmdline, "depart": depart,
                    "etat": etat})
    return vus


def _racine_du_processus(cwd, home: Path) -> Path | None:
    """Racine `rN` de `home` qui contient `cwd`, ou None si le doute reste.

    L'attribution n'est fiable que si `cwd` tombe sous une racine de runner
    marquée par `.runner`. `home` lui-même, un chemin hors de `home` ou un
    enfant sans marqueur ne permettent pas de dire à quel runner appartient
    le processus : l'appelant préserve alors le cache.
    """
    if cwd is None:
        return None
    try:
        relatif = Path(cwd).relative_to(home)
    except (TypeError, ValueError):
        return None
    if not relatif.parts:
        return None
    candidat = home / relatif.parts[0]
    try:
        marqueur = candidat / ".runner"
        if marqueur.is_file() and not marqueur.is_symlink():
            return candidat
    except OSError:
        return None
    return None


def _racine_executable(cmdline, home: Path) -> Path | None:
    """Racine lue dans le chemin de l'exécutable (`rN/bin/Runner.Worker`)."""
    if not cmdline:
        return None
    return _racine_du_processus(cmdline[0], home)


def _verdict_processus(cache: Path, root: Path, processus, maintenant: float):
    """Raison de préserver le cache si un processus vivant le référence.

    Un Worker est rattaché à sa racine par son cwd réel — la flotte observée
    le lance depuis `root` ou `root/bin` (`bin.<version>`), pas seulement
    depuis `root/_work/...` — ou, si le cwd est illisible, par le chemin de
    son exécutable. Un shell dont la cmdline ou les descripteurs sont
    illisibles peut être `_update.sh` : il préserve. Une attribution
    impossible préserve le cache ; un processus d'une autre racine ne le fait
    pas (#5635, canon §2).
    """
    home = root.parent
    for processus_vu in processus:
        pid = processus_vu.get("pid")
        if processus_vu.get("etat") == "Z":
            continue
        comm = processus_vu.get("comm") or ""
        cwd = processus_vu.get("cwd")
        descripteurs = processus_vu.get("fds")
        cmdline = processus_vu.get("cmdline")
        if descripteurs is None and comm.startswith(
                ("Runner.Listener", "RunnerService")):
            return "descripteurs du Listener illisibles (pid %s)" % pid
        if comm == "Runner.Worker":
            proprietaire = _racine_du_processus(cwd, home)
            if proprietaire is None:
                proprietaire = _racine_executable(cmdline, home)
            if proprietaire is None or proprietaire == root:
                if cwd is None:
                    return "Worker actif, cwd illisible (pid %s)" % pid
                return "Worker actif (pid %s)" % pid
        if comm in INTERPRETEURS_SHELL and cmdline is None:
            return "commande d'un shell illisible (pid %s)" % pid
        if _sous(cwd, cache):
            return "updater vivant (pid %s)" % pid
        if descripteurs is None:
            if comm in INTERPRETEURS_SHELL:
                return "descripteurs d'un shell illisibles (pid %s)" % pid
        else:
            for descripteur in descripteurs:
                if _sous(descripteur, cache):
                    return "updater vivant (pid %s, descripteur ouvert)" % pid
        for argument in cmdline or []:
            if argument.endswith("/_work/_update.sh") and str(root) in argument:
                return "script de mise à jour vivant (pid %s)" % pid
        if (comm == "Runner.Listener" or comm.startswith("RunnerService")
                or comm == "node") and cwd == root:
            depart = processus_vu.get("depart")
            if depart is None:
                return "démarrage du Listener illisible (pid %s)" % pid
            if maintenant - depart < DELAI_REDEMARRAGE_S:
                minutes = max(0, int((maintenant - depart) / 60))
                return "unité réactivée il y a %d min (pid %s)" % (minutes, pid)
    return None


def _dossier_interne(root: Path, nom: str) -> Path | None:
    """`root/nom` réel ou lien interne à la racine, jamais un lien qui sort."""
    chemin = root / nom
    try:
        if chemin.is_symlink():
            cible = Path(os.path.realpath(chemin))
            if cible.parent != root or not cible.is_dir():
                return None
            return cible
        if chemin.is_dir():
            return chemin
    except OSError:
        return None
    return None


def _references_cache(root: Path, cache: Path) -> str | None:
    """Refuse si bin/externals ou une version pointent dans le cache."""
    for motif in ("bin", "externals", "bin.*", "externals.*"):
        for chemin in root.glob(motif):
            if not chemin.is_symlink():
                continue
            try:
                cible = Path(os.path.realpath(chemin))
            except OSError:
                continue
            if _sous(cible, cache):
                return "référencé par %s" % chemin.name
    return None


def _verifier_cache(cache: Path, home: Path) -> str | None:
    """None si le chemin est un cache `_update` attendu, sinon la raison."""
    try:
        if cache.is_symlink() or not cache.is_dir():
            return "lien symbolique ou dossier absent"
        travail = cache.parent
        root = travail.parent
        if travail.name != "_work" or root.parent != home:
            return "chemin inattendu"
        for composant in (root, travail):
            if composant.is_symlink():
                return "lien symbolique dans le chemin"
        config = root / ".runner"
        if config.is_symlink() or not config.is_file():
            return "pas une racine de runner (.runner absent)"
        if cache.stat().st_dev != travail.stat().st_dev:
            return "point de montage"
    except OSError as erreur:
        return "chemin illisible (%s)" % erreur
    try:
        entrees = list(os.scandir(cache))
    except OSError as erreur:
        return "cache illisible (%s)" % erreur
    try:
        peripherique = cache.stat().st_dev
    except OSError as erreur:
        return "cache illisible (%s)" % erreur
    for entree in entrees:
        if entree.is_symlink():
            return "lien symbolique en tête (%s)" % entree.name
        if entree.name not in ENTREES_UPDATE and not ARCHIVE_UPDATE.match(entree.name):
            return "entrée inattendue (%s)" % entree.name
        try:
            if entree.stat(follow_symlinks=False).st_dev != peripherique:
                return "point de montage dans le cache (%s)" % entree.name
        except OSError as erreur:
            return "entrée illisible (%s)" % erreur
    for nom in ("bin", "externals"):
        dossier = cache / nom
        if dossier.is_symlink() or not dossier.is_dir():
            return "%s absent ou lié" % nom
    return None


def _lien_sortant(chemin: Path, cache: Path) -> str | None:
    """Refuse un lien absolu ou qui sort du cache ; les liens du paquet restent."""
    try:
        cible = os.readlink(chemin)
    except OSError as erreur:
        return "lien illisible (%s)" % erreur
    if not cible or os.path.isabs(cible):
        return "lien absolu ou vide (%s)" % chemin.name
    relatif = os.path.normpath(os.path.join(
        os.path.dirname(str(chemin.relative_to(cache))), cible))
    if relatif == os.pardir or relatif.startswith(os.pardir + os.sep):
        return "lien sortant (%s)" % chemin.name
    return None


def _verifier_liens(cache: Path) -> str | None:
    """Refuse un lien externe, sans suivre ni exécuter aucun lien du paquet."""
    try:
        peripherique = cache.lstat().st_dev
    except OSError as erreur:
        return "cache illisible (%s)" % erreur
    total = 0
    erreurs = []
    for racine, dossiers, fichiers in os.walk(
            cache, topdown=True, followlinks=False, onerror=erreurs.append):
        try:
            if os.lstat(racine).st_dev != peripherique:
                return "point de montage dans le cache"
        except OSError as erreur:
            return "arborescence illisible (%s)" % erreur
        total += len(dossiers) + len(fichiers)
        if total > MAX_ENTREES_UPDATE:
            return "arborescence trop grande"
        for nom in list(dossiers):
            chemin = Path(racine) / nom
            if chemin.is_symlink():
                raison = _lien_sortant(chemin, cache)
                if raison:
                    return raison
                dossiers.remove(nom)
            else:
                try:
                    if os.lstat(chemin).st_dev != peripherique:
                        return "point de montage dans le cache (%s)" % nom
                except OSError as erreur:
                    return "arborescence illisible (%s)" % erreur
        for nom in fichiers:
            chemin = Path(racine) / nom
            if chemin.is_symlink():
                raison = _lien_sortant(chemin, cache)
                if raison:
                    return raison
            else:
                try:
                    if os.lstat(chemin).st_dev != peripherique:
                        return "point de montage dans le cache (%s)" % nom
                except OSError as erreur:
                    return "arborescence illisible (%s)" % erreur
    if erreurs:
        return "arborescence illisible (%s)" % erreurs[0]
    return None


def _taille_arbre(chemin: Path) -> int:
    """Octets réellement occupés (blocs de 512), sans suivre les liens.

    Un fichier à plusieurs liens durs ne libère rien tant qu'un autre nom
    existe : il n'est pas compté (mesure prudente, le `df` avant/après fait foi).
    """
    if chemin.is_symlink():
        return 0
    total = 0
    for racine, dossiers, fichiers in os.walk(chemin, followlinks=False):
        try:
            total += os.lstat(racine).st_blocks * 512
        except OSError:
            pass
        for nom in fichiers:
            try:
                info = os.lstat(os.path.join(racine, nom))
            except OSError:
                continue
            if info.st_nlink > 1:
                continue
            blocs = getattr(info, "st_blocks", None)
            total += blocs * 512 if blocs is not None else info.st_size
        for nom in dossiers:
            chemin_lien = os.path.join(racine, nom)
            if os.path.islink(chemin_lien):
                try:
                    total += os.lstat(chemin_lien).st_blocks * 512
                except OSError:
                    pass
    return total


def _supprimer_arbre(chemin: Path) -> None:
    """Supprime sans suivre aucun lien ni franchir un point de montage."""
    if chemin.is_symlink():
        raise OSError("lien symbolique")
    peripherique = chemin.lstat().st_dev
    for racine, dossiers, fichiers in os.walk(
            chemin, topdown=False, followlinks=False):
        if os.lstat(racine).st_dev != peripherique:
            raise OSError("point de montage dans le cache")
        for nom in fichiers:
            try:
                os.unlink(os.path.join(racine, nom))
            except FileNotFoundError:
                pass
        for nom in dossiers:
            chemin_lien = os.path.join(racine, nom)
            try:
                if os.path.islink(chemin_lien):
                    os.unlink(chemin_lien)
                elif os.lstat(chemin_lien).st_dev != peripherique:
                    raise OSError("point de montage dans le cache")
                else:
                    os.rmdir(chemin_lien)
            except FileNotFoundError:
                pass
    os.rmdir(chemin)


def _installation_complete(root: Path, version: str, cache: Path) -> str | None:
    """Vérifie que bin/externals installés sont bien la copie de ce cache."""
    bin_dir = _dossier_interne(root, "bin")
    externals_dir = _dossier_interne(root, "externals")
    if bin_dir is None or externals_dir is None:
        return "bin/externals courant absent ou hors racine"
    lien_bin = (root / "bin").is_symlink()
    lien_externals = (root / "externals").is_symlink()
    if lien_bin != lien_externals:
        return "bin/externals mixtes (lien et dossier)"
    if lien_bin:
        if (root / "bin").resolve().name != "bin." + version:
            return "lien bin inattendu"
        if (root / "externals").resolve().name != "externals." + version:
            return "lien externals inattendu"
    empreinte_installee = _empreinte(bin_dir / "Runner.Listener.deps.json")
    empreinte_cache = _empreinte(cache / "bin" / "Runner.Listener.deps.json")
    if empreinte_installee is None or empreinte_cache is None:
        return "empreinte de version illisible"
    if empreinte_installee != empreinte_cache:
        return "contenu du cache différent de la version installée"
    return None


def _decision_cache(cache: Path, root: Path, home: Path, processus,
                    maintenant: float) -> dict:
    """Verdict motivé pour un cache : éligible, préservé ou refusé."""
    def decision(verdict, raison=None):
        return {"cache": str(cache), "root": str(root), "verdict": verdict,
                "raison": raison, "version": None, "courante": None,
                "octets": None}

    raison = _verifier_cache(cache, home)
    if raison:
        return decision("refusé", raison)
    raison = _references_cache(root, cache)
    if raison:
        return decision("refusé", raison)
    courante = _version_installee(root)
    if courante is None:
        return decision("préservé", "version courante illisible")
    cachee = _lire_version(cache)
    if cachee is None:
        return decision("préservé", "version du cache illisible")
    resultat = decision("éligible")
    resultat["version"] = cachee
    resultat["courante"] = courante
    version_cachee = _version_tuple(cachee)
    version_courante = _version_tuple(courante)
    if version_cachee is None or version_courante is None:
        resultat["verdict"] = "préservé"
        resultat["raison"] = "version illisible"
        return resultat
    if version_cachee > version_courante:
        resultat["verdict"] = "préservé"
        resultat["raison"] = "mise à jour en attente (%s > %s)" % (
            cachee, courante)
        return resultat
    raison = _verdict_processus(cache, root, processus, maintenant)
    if raison:
        resultat["verdict"] = "préservé"
        resultat["raison"] = raison
        return resultat
    if cachee == courante:
        raison = _installation_complete(root, courante, cache)
        if raison:
            resultat["verdict"] = "préservé"
            resultat["raison"] = raison
            return resultat
    raison = _verifier_liens(cache)
    if raison:
        resultat["verdict"] = "refusé"
        resultat["raison"] = raison
    return resultat


def _version_installee(root: Path) -> str | None:
    dossier = _dossier_interne(root, "bin")
    return None if dossier is None else _lire_version_bin(dossier)


def _retirer_cache(cache: Path, root: Path, home: Path,
                   maintenant: float) -> tuple[int, str | None]:
    """Recontrôle tout, puis retire le cache ; jamais sur un état périmé."""
    try:
        avant = cache.lstat()
    except OSError as erreur:
        return 0, "cache illisible (%s)" % erreur
    processus = _processus(maintenant=maintenant)
    decision = _decision_cache(cache, root, home, processus, maintenant)
    if decision["verdict"] != "éligible":
        return 0, decision.get("raison") or "état non prouvé"
    try:
        apres = cache.lstat()
    except OSError:
        return 0, "cache disparu entre deux contrôles"
    if (apres.st_dev, apres.st_ino) != (avant.st_dev, avant.st_ino):
        return 0, "cache remplacé entre deux contrôles"
    taille = _taille_arbre(cache)
    try:
        _supprimer_arbre(cache)
    except OSError as erreur:
        reste = _taille_arbre(cache) if cache.exists() else 0
        return max(0, taille - reste), "suppression partielle : %s" % erreur
    return taille, None


def _resume_caches(decisions, dry_run: bool) -> str:
    if not decisions:
        return "aucun cache _update"
    morceaux = []
    for element in decisions:
        nom = Path(element["root"]).name
        if element["verdict"] == "retiré":
            morceaux.append("%s retiré (%s)" % (nom, gib(element.get("octets"))))
        elif element["verdict"] == "serait retiré":
            morceaux.append("%s serait retiré (%s)"
                            % (nom, gib(element.get("octets"))))
        else:
            morceaux.append("%s %s : %s"
                            % (nom, "préservé" if element["verdict"] == "préservé"
                               else "refusé", element.get("raison")))
    return " ; ".join(morceaux)


def _caches_update(racine: Path | None, dry_run: bool = False, *,
                   processus=None, maintenant: float | None = None) -> dict:
    """Politique #5635 : ne retire que les caches dont la version est installée."""
    if racine is None:
        return {"etape": "caches _update inactifs", "libere": 0,
                "sortie": "RUNNER_WORKSPACE absent, étape sautée", "caches": []}
    home = Path(os.path.abspath(racine.parent))
    maintenant = time.time() if maintenant is None else maintenant
    if processus is None:
        processus = _processus(maintenant=maintenant)
    decisions = []
    liberes = retires = 0
    for cache in sorted(home.glob("*/_work/_update")):
        root = cache.parent.parent
        try:
            decision = _decision_cache(cache, root, home, processus, maintenant)
        except Exception as erreur:  # noqa: BLE001 — un cache ne fait pas un verdict
            decision = {"cache": str(cache), "root": str(root),
                        "verdict": "préservé", "version": None, "courante": None,
                        "octets": None,
                        "raison": "contrôle impossible : %s" % erreur}
        if decision["verdict"] == "éligible":
            decision["octets"] = _taille_arbre(cache)
            if dry_run:
                decision["verdict"] = "serait retiré"
                retires += 1
            else:
                obtenu, raison = _retirer_cache(cache, root, home, maintenant)
                decision["octets"] = obtenu
                liberes += obtenu
                if raison:
                    decision["verdict"] = "préservé"
                    decision["raison"] = raison
                else:
                    decision["verdict"] = "retiré"
                    retires += 1
        decisions.append(decision)
    return {"etape": "caches _update inactifs", "libere": liberes,
            "sortie": _resume_caches(decisions, dry_run), "caches": decisions,
            "retires": retires}


def _updates(dry_run: bool) -> dict:
    racine = _racine_runner()
    if racine is None:
        return {"etape": "caches _update inactifs", "libere": 0,
                "sortie": "RUNNER_WORKSPACE absent, étape sautée"}
    resultat = _caches_update(racine, dry_run)
    return {"etape": "caches _update inactifs", "libere": resultat["libere"],
            "sortie": resultat["sortie"]}


def menage(dry_run: bool = False, force: bool = False,
           images_inutilisees: bool = False) -> int:
    if os.environ.get("GITHUB_ACTIONS") != "true" and not force:
        print("ménage : hors CI (GITHUB_ACTIONS != true), rien n'est supprimé ; "
              "--force pour un essai explicite")
        return 0
    pret, version = docker_pret()
    if not pret:
        print("ménage : docker indisponible (%s), les étapes docker sont sautées" % version)
    verrou = _verrou()
    if verrou is None:
        return 0
    try:
        avant = espace("/")
        print("ménage de %s (%s) : %s libres avant (docker %s)"
              % (socket.gethostname(), os.environ.get("RUNNER_NAME", "runner inconnu"),
                 gib(avant["libre"]), version))
        etapes = []
        if pret:
            etapes += [_conteneurs, _volumes, _images]
            if images_inutilisees:
                etapes.append(_images_inutilisees)
            etapes.append(_cache_build)
        etapes += [_temp, _diag, _updates, _tmp]
        resultats = []
        for etape in etapes:
            try:
                resultat = etape(dry_run)
            except Exception as erreur:  # noqa: BLE001 — jamais un verdict
                resultat = {"etape": etape.__name__, "libere": 0,
                            "sortie": "échec inattendu : %s" % erreur}
            resultats.append(resultat)
            detail = ("— " + resultat["sortie"]) if resultat["sortie"] else ""
            print("ménage %-32s %s %s" % (resultat["etape"], gib(resultat["libere"]), detail))
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
    mesure_parser = sous.add_parser("mesure", help="sonde disque structurée sans docker")
    mesure_parser.add_argument("--host", help="nom Host du mesh, lié au label de la VM")
    menage_parser = sous.add_parser("menage", help="élague ce qui n'est plus utilisé")
    menage_parser.add_argument("--dry-run", action="store_true",
                               help="imprime les commandes, ne supprime rien")
    menage_parser.add_argument("--force", action="store_true",
                               help="accepte de tourner hors CI (essai explicite)")
    menage_parser.add_argument("--images-inutilisees", action="store_true",
                               help="élague aussi les images sans conteneur de plus de 7 jours "
                                    "(ménage dédié ; jamais dans un job de tests)")
    return analyseur.parse_args(argv)


def main(argv=None) -> int:
    args = _analyse(argv)
    if args.mode == "mesure":
        mesure(args.host)
        return 0
    if args.mode == "rapport":
        rapport(complet=args.complet, seuil_gib=args.seuil_gib)
        return 0
    return menage(dry_run=args.dry_run, force=args.force,
                  images_inutilisees=args.images_inutilisees)


if __name__ == "__main__":
    sys.exit(main())
