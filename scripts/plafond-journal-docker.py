#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#7962 — plafonne le journal json-file d'un conteneur Docker, sans le redémarrer.

Le pilote `json-file` de Docker écrit chaque ligne dans un fichier unique et
n'applique aucun plafond quand le conteneur a été créé sans `--log-opt
max-size` (c'est le cas du banc `ameesh-pg` d'AG2 : `LogConfig` vide). Le
fichier a atteint 1,24 Go en 7 jours. Ce programme le borne sans arrêter ni
recréer le conteneur : au-delà du seuil, il copie la fin du journal, la
compresse en 0600 dans un répertoire d'état, puis tronque le fichier à zéro.

La troncature est sûre parce que le pilote tient le fichier ouvert en
`O_APPEND` : la prochaine écriture repart à l'offset 0 du même inode, sans
erreur ni trou. `O_APPEND` ne dépend pas du programme qui tronque, il est
visible dans `/proc/<dockerd>/fdinfo/<fd>` (`flags: 02102001`, soit
O_WRONLY|O_LARGEFILE|O_APPEND|O_CLOEXEC) ; la preuve AG2 du 20/11/2026 est
consignée dans l'en-tête de la minuterie (`deploy/systemd/`).

Le programme ne touche qu'un journal `json-file` situé sous
`<DockerRootDir>/containers/<id>/<id>-json.log`, refuse un lien symbolique ou
un fichier remplacé entre-temps, et prend un verrou pour qu'une seule passe
tronque à la fois. Il ne redémarre jamais Docker ni le conteneur.

Usage :
    plafond-journal-docker.py --conteneur ameesh-pg
    plafond-journal-docker.py --conteneur ameesh-pg --etat --json
    plafond-journal-docker.py --conteneur ameesh-pg --seuil 1048576 --dry-run

Les tailles sont en octets ; `--seuil` et `--garder` acceptent les suffixes
K/M/G (binaires) pour la commodité de la ligne de commande.
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

SCHEMA = "ameesh-journal-conteneur/1"
SEUIL_DEFAUT = 128 * 1024 * 1024
GARDER_DEFAUT = 8 * 1024 * 1024
ARCHIVES_DEFAUT = 8
DOCKER_DEFAUT = os.environ.get("AMEESH_DOCKER", "docker")
TIMEOUT_DOCKER = 10


class Refus(Exception):
    """Le journal ne peut pas être plafonné sans risque : rien n'est touché."""


def horodatage() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def maintenant() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def etat_dir_defaut() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "ameesh", "journal-conteneur")


def taille(texte: str) -> int:
    """Octets, avec suffixe binaire K/M/G (1K = 1024)."""
    suffixes = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
    t = texte.strip().lower()
    if t and t[-1] in suffixes:
        return int(float(t[:-1]) * suffixes[t[-1]])
    return int(t)


def _docker(docker: str, args: list[str]) -> str:
    try:
        proc = subprocess.run([docker, *args], capture_output=True, text=True,
                              timeout=TIMEOUT_DOCKER, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refus(f"{docker} {' '.join(args)} : {exc}") from exc
    if proc.returncode != 0:
        lignes = (proc.stderr or proc.stdout).strip().splitlines()
        detail = lignes[-1] if lignes else f"code {proc.returncode}"
        raise Refus(f"{docker} {' '.join(args)} : {detail}")
    return proc.stdout


def valider_journal(chemin: str, racine: str, conteneur_id: str) -> tuple[str, os.stat_result]:
    """Réel chemin du journal et son stat, ou refus.

    On n'accepte qu'un fichier régulier, non symbolique, sous
    `<DockerRootDir>/containers/<id>/`, nommé `<id>-json.log`.
    """
    if not chemin:
        raise Refus("LogPath vide")
    if not os.path.isabs(chemin):
        raise Refus(f"LogPath non absolu : {chemin!r}")
    if os.path.islink(chemin):
        raise Refus(f"journal symbolique refusé : {chemin}")
    try:
        st = os.lstat(chemin)
    except OSError as exc:
        raise Refus(f"journal illisible : {exc}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise Refus(f"journal non régulier : {chemin}")
    reel = os.path.realpath(chemin)
    containers = os.path.join(os.path.realpath(racine), "containers")
    try:
        commun = os.path.commonpath([reel, containers])
    except ValueError as exc:
        raise Refus(f"journal hors DockerRootDir : {chemin}") from exc
    if commun != containers:
        raise Refus(f"journal hors de {containers} : {reel}")
    if os.path.basename(os.path.dirname(reel)) != conteneur_id:
        raise Refus("répertoire du conteneur incohérent avec docker inspect")
    if not reel.endswith("-json.log"):
        raise Refus(f"nom de journal inattendu : {reel}")
    return reel, st


def lire_conteneur(docker: str, conteneur: str) -> dict:
    """État du conteneur, sans aucun geste : deux lectures Docker."""
    try:
        data = json.loads(_docker(docker, ["inspect", "--type", "container", conteneur]))
        c = data[0]
    except (ValueError, IndexError, TypeError) as exc:
        raise Refus(f"docker inspect {conteneur} : réponse illisible") from exc
    host = c.get("HostConfig") or {}
    log = host.get("LogConfig") or {}
    info = {
        "conteneur": conteneur,
        "id": c.get("Id") or "",
        "image": (c.get("Config") or {}).get("Image") or "",
        "running": bool((c.get("State") or {}).get("Running")),
        "log_type": log.get("Type") or "",
        "log_config": log.get("Config") or {},
        "journal": c.get("LogPath") or "",
    }
    if info["log_type"] != "json-file":
        return info
    racine = _docker(docker, ["info", "--format", "{{.DockerRootDir}}"]).strip()
    if not racine:
        raise Refus("docker info : DockerRootDir vide")
    info["journal"], st = valider_journal(info["journal"], racine, info["id"])
    info["octets"] = st.st_size
    return info


def preparer_etat(chemin: str) -> str:
    if os.path.islink(chemin):
        raise Refus(f"répertoire d'état symbolique refusé : {chemin}")
    try:
        os.makedirs(chemin, mode=0o700, exist_ok=True)
        st = os.stat(chemin)
    except OSError as exc:
        raise Refus(f"répertoire d'état : {exc}") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise Refus(f"répertoire d'état non répertoire : {chemin}")
    if st.st_mode & 0o077:
        os.chmod(chemin, 0o700)
    return chemin


def verrouiller(etat: str):
    """Verrou exclusif non bloquant ; None si une passe tient déjà le verrou."""
    chemin = os.path.join(etat, ".verrou")
    fd = os.open(chemin, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def lire_fin(chemin: str, garder: int, attendu: os.stat_result) -> tuple[bytes, bool]:
    """Dernières `garder` octets du journal, et si la copie commence au milieu."""
    fd = os.open(chemin, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != (attendu.st_dev, attendu.st_ino):
            raise Refus("le journal a changé de fichier pendant la lecture")
        debut = max(0, st.st_size - garder)
        if debut:
            os.lseek(fd, debut, os.SEEK_SET)
        morceaux = []
        reste = garder
        while reste > 0:
            bloc = os.read(fd, min(1024 * 1024, reste))
            if not bloc:
                break
            morceaux.append(bloc)
            reste -= len(bloc)
        return b"".join(morceaux), debut > 0
    finally:
        os.close(fd)


def retirer_ligne_partielle(donnees: bytes, partiel: bool) -> bytes:
    """Une copie qui commence au milieu d'une ligne commence après son saut."""
    if not partiel or not donnees:
        return donnees
    saut = donnees.find(b"\n")
    if saut < 0:
        return b""
    return donnees[saut + 1:]


def ecrire_archive(etat: str, conteneur: str, donnees: bytes) -> str:
    base = f"{conteneur}-{horodatage()}"
    dest = os.path.join(etat, base + ".log.gz")
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(etat, f"{base}-{n}.log.gz")
        n += 1
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".log.gz", dir=etat)
    try:
        with os.fdopen(fd, "wb") as brut:
            with gzip.GzipFile(filename="", mode="wb", fileobj=brut,
                               compresslevel=6, mtime=0) as gz:
                gz.write(donnees)
            brut.flush()
            os.fsync(brut.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest


def tronquer(chemin: str, attendu: os.stat_result) -> None:
    fd = os.open(chemin, os.O_WRONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != (attendu.st_dev, attendu.st_ino):
            raise Refus("le journal a changé de fichier avant la troncature")
        os.ftruncate(fd, 0)
        os.fsync(fd)
    finally:
        os.close(fd)
    if os.stat(chemin).st_size != 0:
        raise Refus("troncature non effective")


def lister_archives(etat: str, conteneur: str) -> list[tuple[float, str, int]]:
    if not os.path.isdir(etat):
        return []
    prefixe = conteneur + "-"
    out = []
    for nom in os.listdir(etat):
        if not (nom.startswith(prefixe) and nom.endswith(".log.gz")):
            continue
        chemin = os.path.join(etat, nom)
        try:
            st = os.lstat(chemin)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            out.append((st.st_mtime, nom, st.st_size))
    out.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return out


def elaguer(etat: str, conteneur: str, garder: int) -> list[str]:
    retires = []
    for _, nom, _ in lister_archives(etat, conteneur)[garder:]:
        try:
            os.unlink(os.path.join(etat, nom))
            retires.append(nom)
        except OSError as exc:
            raise Refus(f"élagage de {nom} : {exc}") from exc
    return retires


def plafonner(chemin: str, seuil: int, garder: int, etat: str, conteneur: str,
              garder_archives: int, dry_run: bool = False) -> dict:
    """Archive la fin puis tronque ; ne fait rien sous le seuil."""
    try:
        st = os.lstat(chemin)
    except OSError as exc:
        raise Refus(f"journal illisible : {exc}") from exc
    avant = st.st_size
    res = {
        "action": "rien",
        "octets_avant": avant,
        "octets_apres": avant,
        "octets_gardes": 0,
        "archive": None,
        "sha256": None,
    }
    if avant <= seuil:
        return res
    donnees, partiel = lire_fin(chemin, garder, st)
    donnees = retirer_ligne_partielle(donnees, partiel)
    res["action"] = "tronque"
    res["octets_gardes"] = len(donnees)
    if dry_run:
        return res
    if donnees:
        res["archive"] = ecrire_archive(etat, conteneur, donnees)
        res["sha256"] = hashlib.sha256(donnees).hexdigest()
    tronquer(chemin, st)
    res["octets_apres"] = 0
    res["archives_retirees"] = elaguer(etat, conteneur, garder_archives)
    return res


def etat_complet(info: dict, seuil: int, etat: str, conteneur: str) -> dict:
    archives = lister_archives(etat, conteneur)
    return {
        "schema": SCHEMA,
        "conteneur": conteneur,
        "id": info.get("id", ""),
        "image": info.get("image", ""),
        "running": info.get("running", False),
        "log_type": info.get("log_type", ""),
        "log_config": info.get("log_config", {}),
        "journal": info.get("journal", ""),
        "octets": info.get("octets", 0),
        "seuil": seuil,
        "archives": len(archives),
        "octets_archives": sum(a[2] for a in archives),
        "derniere_archive": archives[0][1] if archives else None,
    }


def texte_etat(e: dict) -> str:
    return (f"{e['conteneur']} : {e['log_type']} {json.dumps(e['log_config'], sort_keys=True)} "
            f"{e['octets']} o / seuil {e['seuil']} o, {e['archives']} archive(s) "
            f"{e['octets_archives']} o\njournal : {e['journal']}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Plafonne le journal json-file d'un conteneur Docker, sans redémarrage.")
    p.add_argument("--conteneur", default="ameesh-pg")
    p.add_argument("--seuil", type=taille, default=SEUIL_DEFAUT,
                   help="taille au-delà de laquelle tronquer (défaut 128M)")
    p.add_argument("--garder", type=taille, default=GARDER_DEFAUT,
                   help="fin conservée compressée avant troncature (défaut 8M)")
    p.add_argument("--archives", type=int, default=ARCHIVES_DEFAUT,
                   help="nombre d'archives gardées (défaut 8)")
    p.add_argument("--etat-dir", default=None)
    p.add_argument("--docker", default=DOCKER_DEFAUT)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--etat", action="store_true", help="lecture seule")
    p.add_argument("--json", action="store_true")
    p.add_argument("--verbose", action="store_true")
    a = p.parse_args(argv)
    if a.seuil < 1:
        p.error("--seuil doit être positif")
    if a.garder < 1:
        p.error("--garder doit être positif")
    if a.archives < 1:
        p.error("--archives doit être positif")
    etat_dir = a.etat_dir or etat_dir_defaut()
    try:
        info = lire_conteneur(a.docker, a.conteneur)
        if a.etat:
            e = etat_complet(info, a.seuil, etat_dir, a.conteneur)
            print(json.dumps(e, ensure_ascii=False) if a.json else texte_etat(e))
            return 0
        if info["log_type"] != "json-file":
            if a.verbose or a.json:
                print(json.dumps({"schema": SCHEMA, "action": "ignore",
                                  "conteneur": a.conteneur,
                                  "log_type": info["log_type"]}))
            return 0
        etat = preparer_etat(etat_dir)
        fd = verrouiller(etat)
        if fd is None:
            if a.verbose:
                print(f"{maintenant()} {a.conteneur} : une autre passe tient le verrou")
            return 0
        try:
            res = plafonner(info["journal"], a.seuil, a.garder, etat, a.conteneur,
                            a.archives, a.dry_run)
        finally:
            os.close(fd)
        res["schema"] = SCHEMA
        res["conteneur"] = a.conteneur
        res["seuil"] = a.seuil
        res["journal"] = info["journal"]
        if a.json:
            print(json.dumps(res, ensure_ascii=False))
        elif res["action"] == "rien":
            if a.verbose:
                print(f"{maintenant()} {a.conteneur} : journal {res['octets_avant']} o "
                      f"<= seuil {a.seuil} o, rien à faire")
        else:
            if res["archive"]:
                archive = os.path.basename(res["archive"])
                taille_archive = os.stat(res["archive"]).st_size
                detail = (f"fin {res['octets_gardes']} o -> {archive} "
                          f"({taille_archive} o, sha256 {res['sha256'][:16]}…)")
            else:
                detail = f"fin {res['octets_gardes']} o, aucune archive"
            prefixe = "dry-run : " if a.dry_run else ""
            print(f"{prefixe}{maintenant()} {a.conteneur} : journal "
                  f"{res['octets_avant']} o -> 0, {detail}")
        return 0
    except Refus as exc:
        print(f"plafond-journal: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"plafond-journal: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
