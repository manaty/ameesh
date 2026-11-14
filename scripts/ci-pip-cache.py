#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#7380 — cache pip des VM de runners : diagnostic et purge des entrées illisibles.

Le 14/11/2026, la part psycopg 5/12 du run 43599097259 (runner
`ameesh-ci-1-8`) a échoué à l'étape « dépendances (pilote psycopg) » : pip a
lu une roue depuis le cache pip et son empreinte ne correspondait pas
(« THESE PACKAGES DO NOT MATCH THE HASHES »). Le cache était
`/home/runner/.cache/pip`, commun aux huit runners de l'hôte ;
`actions/setup-python` y extrait un cache pip à chaque job avec `tar`, non
atomique, pendant que d'autres jobs lisent la même entrée. Le correctif
(`tests.yml`) donne à chaque runner son cache, sous son `_work`.

Ce script, exécuté depuis un job (compte `runner`, sans root), sert à établir
et nettoyer l'état de l'hôte avant et après ce correctif :

* `diagnostic` : liste les caches pip de l'hôte (le cache partagé historique
  `~/.cache/pip` et les caches par runner `<runner>/_work/pip-cache`), vérifie
  l'intégrité de chaque roue du cache HTTP (archive zip, CRC, puis empreintes
  `sha256` du `RECORD` de la roue) et nomme les entrées illisibles ;
* `purge` : supprime les entrées dont la roue est **prouvée** illisible (le
  corps et ses métadonnées) ; `--dry-run` dit ce qui partirait sans rien
  supprimer. Une entrée seulement douteuse (RECORD absent ou illisible) est
  signalée mais gardée : on ne supprime que sur une preuve.

Les empreintes du `RECORD` d'une roue sont en base64 urlsafe sans `=`
(PEP 427) ; les outils anciens y écrivaient de l'hexadécimal. Les deux formes
sont lues. Une entrée n'est jamais réparée : pip retélécharge la roue et la
remet en cache, atomiquement. Le script ne touche à rien hors d'un cache pip
(`http-v2`).

Usage :
  scripts/ci-pip-cache.py diagnostic
  scripts/ci-pip-cache.py purge [--dry-run]
"""
from __future__ import annotations

import argparse
import base64
import binascii
import csv
import hashlib
import io
import os
import re
import socket
import sys
import zipfile
import zlib
from pathlib import Path

#: Le cache pip par défaut de l'utilisateur runner (partagé par les huit
#: runners d'un hôte avant #7380).
CACHE_PARTAGE = Path.home() / ".cache" / "pip"
#: Le cache par runner posé par tests.yml : `<runner>/_work/pip-cache`.
NOM_CACHE_RUNNER = "pip-cache"
MAGIE_ZIP = b"PK\x03\x04"
HEX_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def caches_pip(environ=None) -> list[tuple[str, Path]]:
    """Les caches pip à inspecter, sans doublon, dans un ordre stable.

    `PIP_CACHE_DIR` (posé par le job), le cache partagé historique, puis les
    caches par runner des huit runners de l'hôte quand ils existent. Un même
    chemin n'est rendu qu'une fois : un job dont PIP_CACHE_DIR est déjà le
    cache par runner ne le voit pas deux fois.
    """
    environ = os.environ if environ is None else environ
    vus: set[str] = set()
    sortie: list[tuple[str, Path]] = []

    def ajouter(libelle: str, chemin: Path) -> None:
        try:
            cle = str(chemin.expanduser().resolve())
        except OSError:
            cle = str(chemin)
        if cle in vus:
            return
        vus.add(cle)
        sortie.append((libelle, chemin.expanduser()))

    dirige = environ.get("PIP_CACHE_DIR")
    if dirige:
        ajouter("cache pip du job (PIP_CACHE_DIR)", Path(dirige))
    ajouter("cache pip partagé (historique)", CACHE_PARTAGE)
    for base in (Path("/home/runner"), Path("/Users/runner")):
        for cache in sorted(base.glob("r*/_work/" + NOM_CACHE_RUNNER)):
            ajouter("cache pip par runner (%s)" % cache.parent.parent.name, cache)
    return sortie


def entrees_http(cache: Path) -> list[Path]:
    """Les corps du cache HTTP de pip (`http-v2`), triés."""
    base = cache / "http-v2"
    if not base.is_dir():
        return []
    return sorted(chemin for chemin in base.rglob("*.body") if chemin.is_file())


def _empreinte_sha256(flux) -> bytes:
    h = hashlib.sha256()
    while True:
        bloc = flux.read(1 << 20)
        if not bloc:
            return h.digest()
        h.update(bloc)


def _digest_record(valeur: str) -> bytes | None:
    """Décode l'empreinte `sha256` d'une ligne de RECORD, None si illisible.

    PEP 427 : base64 urlsafe sans remplissage. Quelques outils anciens
    écrivaient l'hexadécimal ; les deux formes sont acceptées.
    """
    valeur = valeur.strip()
    if HEX_SHA256.match(valeur):
        return bytes.fromhex(valeur)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", valeur):
        return None
    try:
        brut = base64.urlsafe_b64decode(valeur + "=" * (-len(valeur) % 4))
    except (ValueError, binascii.Error):
        return None
    return brut if len(brut) == 32 else None


def _digest_court(digest: bytes) -> str:
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")[:12]


def verifier_roue(chemin: Path) -> tuple[str, str | None, bool]:
    """(nom de la roue, raison d'illisibilité ou None, corruption prouvée).

    Une roue saine est une archive zip dont le CRC passe et dont chaque fichier
    listé dans son `RECORD` porte l'empreinte `sha256` annoncée. Le troisième
    booléen n'est vrai que sur une preuve : un `RECORD` absent ou illisible est
    signalé mais ne suffit pas à supprimer l'entrée. Un corps qui n'est pas une
    roue (page d'index, JSON, sdist) est rendu `("", None, False)`.
    """
    nom = ""
    try:
        with open(chemin, "rb") as flux:
            if flux.read(4) != MAGIE_ZIP:
                return "", None, False
        with zipfile.ZipFile(chemin) as archive:
            # La roue peut VENDORISER d'autres distributions
            # (`setuptools/_vendor/autocommand-…dist-info/`) : leur RECORD est
            # relatif à leur dossier et ne décrit pas l'archive. Seul le
            # dist-info à la racine est celui de la roue.
            for membre in archive.namelist():
                if membre.count("/") == 1 and membre.endswith(".dist-info/METADATA"):
                    nom = membre.split("/")[0]
                    if nom.endswith(".dist-info"):
                        nom = nom[: -len(".dist-info")]
                    break
            mauvais = archive.testzip()
            if mauvais:
                return nom, "CRC invalide pour %s" % mauvais, True
            record = next((m for m in archive.namelist()
                           if m.count("/") == 1
                           and m.endswith(".dist-info/RECORD")), None)
            if record is None:
                return nom, "roue sans RECORD", False
            with archive.open(record) as flux:
                lignes = list(csv.reader(io.TextIOWrapper(
                    flux, encoding="utf-8", errors="replace")))
            for champs in lignes:
                if len(champs) < 2 or not champs[0] or champs[0] == record:
                    continue
                empreinte = champs[1].strip()
                if not empreinte.startswith("sha256="):
                    continue
                attendu = _digest_record(empreinte.split("=", 1)[1])
                if attendu is None:
                    return nom, "empreinte RECORD illisible pour %s" % champs[0], False
                try:
                    with archive.open(champs[0]) as membre:
                        obtenu = _empreinte_sha256(membre)
                except KeyError:
                    return nom, "RECORD nomme %s, absent de l'archive" % champs[0], True
                if obtenu != attendu:
                    return nom, "%s : sha256 %s… au lieu de %s…" % (
                        champs[0], _digest_court(obtenu), _digest_court(attendu)), True
        return nom, None, False
    except (zipfile.BadZipFile, zlib.error, EOFError):
        return nom, "archive zip tronquée ou illisible", True
    except OSError as erreur:
        return nom, "fichier illisible : %s" % erreur, False


def examiner(caches: list[tuple[str, Path]]) -> dict:
    """L'état des caches : comptes, roues et entrées illisibles."""
    etat = {"caches": [], "corps": 0, "roues": 0, "illisibles": []}
    for libelle, cache in caches:
        corps = entrees_http(cache)
        roues = 0
        illisibles = []
        for corps_path in corps:
            nom, raison, prouvee = verifier_roue(corps_path)
            if nom:
                roues += 1
            if raison:
                illisibles.append({"chemin": corps_path, "nom": nom,
                                   "raison": raison, "corrompue": prouvee})
        etat["caches"].append({"libelle": libelle, "chemin": cache,
                               "corps": len(corps), "roues": roues,
                               "illisibles": illisibles})
        etat["corps"] += len(corps)
        etat["roues"] += roues
        etat["illisibles"].extend(illisibles)
    return etat


def _nom_affiche(nom: str, chemin: Path) -> str:
    return nom or chemin.name


def diagnostic(caches: list[tuple[str, Path]]) -> dict:
    etat = examiner(caches)
    print("hôte %s, runner %s : %d cache(s) pip"
          % (socket.gethostname(), os.environ.get("RUNNER_NAME", "?"),
             len(etat["caches"])))
    for bloc in etat["caches"]:
        print("cache pip %s : %s — %d corps, %d roues, %d illisible(s)"
              % (bloc["libelle"], bloc["chemin"], bloc["corps"], bloc["roues"],
                 len(bloc["illisibles"])))
        for entree in bloc["illisibles"]:
            etat_entree = "corrompue" if entree["corrompue"] else "à vérifier"
            print("  %s : %s (%s) — %s"
                  % (etat_entree, _nom_affiche(entree["nom"], entree["chemin"]),
                     entree["chemin"], entree["raison"]))
    if not etat["illisibles"]:
        print("aucune roue illisible dans %d cache(s) (%d corps, %d roues)"
              % (len(etat["caches"]), etat["corps"], etat["roues"]))
    return etat


def purger(caches: list[tuple[str, Path]], dry_run: bool = False) -> dict:
    """Supprime les entrées prouvées corrompues ; rend le compte et les échecs."""
    etat = examiner(caches)
    supprimees = 0
    a_verifier = 0
    echecs = []
    for entree in etat["illisibles"]:
        chemin = entree["chemin"]
        etiquette = _nom_affiche(entree["nom"], chemin)
        if not entree["corrompue"]:
            a_verifier += 1
            print("gardée (à vérifier) : %s (%s) — %s"
                  % (etiquette, chemin, entree["raison"]))
            continue
        if dry_run:
            print("partirait : %s (%s) — %s" % (etiquette, chemin, entree["raison"]))
            continue
        try:
            chemin.unlink()
        except FileNotFoundError:
            # déjà purgée par un autre job de la passe : rien à faire.
            pass
        except OSError as erreur:
            echecs.append((chemin, str(erreur)))
            print("échec : %s (%s) — %s" % (etiquette, chemin, erreur))
            continue
        # La métadonnée est le même nom sans le suffixe `.body`.
        try:
            chemin.with_suffix("").unlink()
        except FileNotFoundError:
            pass
        except OSError as erreur:
            echecs.append((chemin.with_suffix(""), str(erreur)))
            print("échec métadonnée : %s (%s) — %s"
                  % (etiquette, chemin.with_suffix(""), erreur))
            continue
        supprimees += 1
        print("purgée : %s (%s) — %s" % (etiquette, chemin, entree["raison"]))
    if dry_run:
        print("%d entrée(s) corrompue(s) seraient purgée(s), %d à vérifier"
              % (sum(1 for e in etat["illisibles"] if e["corrompue"]), a_verifier))
    else:
        print("%d entrée(s) purgée(s), %d gardée(s) à vérifier, %d échec(s)"
              % (supprimees, a_verifier, len(echecs)))
    return {"supprimees": supprimees, "a_verifier": a_verifier,
            "echecs": echecs, "illisibles": etat["illisibles"]}


def _analyse(argv=None) -> argparse.Namespace:
    analyseur = argparse.ArgumentParser(
        description="cache pip des runners de CI : diagnostic et purge (#7380)")
    sous = analyseur.add_subparsers(dest="commande", required=True)
    sous.add_parser("diagnostic", help="inspecter les caches pip de l'hôte")
    purge = sous.add_parser("purge", help="supprimer les roues prouvées illisibles")
    purge.add_argument("--dry-run", action="store_true",
                       help="dire ce qui partirait, sans supprimer")
    return analyseur.parse_args(argv)


def main(argv=None) -> int:
    arguments = _analyse(argv)
    caches = caches_pip()
    if arguments.commande == "diagnostic":
        diagnostic(caches)
        return 0
    resultat = purger(caches, dry_run=arguments.dry_run)
    return 1 if resultat["echecs"] else 0


if __name__ == "__main__":
    sys.exit(main())
