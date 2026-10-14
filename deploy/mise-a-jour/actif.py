#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Attestation « staging actif » (#1323).

Appelee par `deploy/mise-a-jour/staging.sh` a la fin d'une sequence staging
reussie (#1155), cette sonde lit le journal JSONL de la sequence et n'ecrit
le signal que si l'ACTIVATION est prouvee, hote par hote :

* `installer`, `redemarrer`, `controler` et `sante` tous « ok » dans la
  fenetre de cette execution (depuis `--depuis`) ;
* aucun `retour` (rollback) dans cette fenetre ;
* le commit relu sur l'hote apres la sequence est le commit vise (`--hote
  nom=sha_actif`, comparaison par prefixe : le poste publie un nom court).

Le signal est durable — une attestation JSON sous
`~/.local/state/ameesh/mise-a-jour/actif-<sha>.json` — et borne : un courrier
par rapporteur, jamais deux fois pour le meme commit. Une preuve manquante
n'ecrit ni attestation ni courrier de reussite. Aucun lot n'est clos
automatiquement et aucun sondage n'est arme : le rapporteur reprend sa
verification apres deploiement lui-meme.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from ameesh import config, db as db_mod, mail, work

SCHEMA = "ameesh-staging-actif/1"
#: hôtes du staging complet (canon §14) : un sous-ensemble n'atteste rien
HOTES_CANON = ("ameesh-agents-1", "ameesh-agents-2", "manaty-mesh-1", "pc-smichea")
#: etapes qui, par hote, prouvent l'activation et pas seulement l'installation
ETAPES_REQUISES = ("installer", "redemarrer", "controler", "sante")
#: etat par defaut des attestations (meme arbre que le journal de staging)
ETAT_DEFAUT = Path.home() / ".local/state/ameesh/mise-a-jour"


class PreuveManquante(Exception):
    """Une preuve d'activation manque : aucun signal de reussite."""


def lire_journal(chemin: Path, depuis: str) -> list[dict]:
    """Evenements du journal a partir de `depuis` (ISO UTC, ordre lexical).

    Le journal est quotidien : une execution precedente du meme jour ne doit
    pas disqualifier celle-ci. Un journal illisible ne prouve rien.
    """
    try:
        lignes = chemin.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PreuveManquante("journal illisible (%s) : %s" % (chemin, exc))
    evenements = []
    for ligne in lignes:
        if not ligne.strip():
            continue
        try:
            evenement = json.loads(ligne)
        except json.JSONDecodeError as exc:
            raise PreuveManquante("journal illisible (%s) : %s" % (chemin, exc))
        if str(evenement.get("ts") or "") >= depuis:
            evenements.append(evenement)
    if not evenements:
        raise PreuveManquante("journal vide depuis %s : %s" % (depuis, chemin))
    return evenements


def preuves(evenements: list[dict], sha: str, actifs: dict[str, str]) -> dict:
    """{hote: preuves} pour la sequence attestee, ou PreuveManquante.

    La comparaison du commit actif est un prefixe dans les deux sens : le
    poste publie un nom court (`src-<7>`), les VM un SHA complet.
    """
    if sorted(actifs) != sorted(HOTES_CANON):
        raise PreuveManquante(
            "sequence partielle (%s) : un staging complet des quatre hotes est requis"
            % ", ".join(sorted(actifs)))
    retours = [e for e in evenements if e.get("etape") == "retour"]
    if retours:
        raise PreuveManquante("retour/rollback present dans la fenetre (%d evenement(s))"
                              % len(retours))
    fins = [e for e in evenements if e.get("etape") == "sequence"]
    if not fins or fins[-1].get("verdict") != "ok":
        raise PreuveManquante("sequence staging non terminee (verdict ok absent)")
    sha = sha.lower()
    resultat = {}
    for hote in HOTES_CANON:
        actif = (actifs.get(hote) or "").lower()
        if not actif:
            raise PreuveManquante("%s : commit actif illisible" % hote)
        if len(actif) > len(sha) or not sha.startswith(actif):
            raise PreuveManquante("%s : commit actif %s different de la cible %s"
                                  % (hote, actif[:12], sha[:12]))
        # dernier verdict par etape, apres le dernier « ko » de l'hote : un ko
        # suivi d'un retour est deja disqualifie plus haut.
        siens = [e for e in evenements if e.get("hote") == hote]
        ko = [i for i, e in enumerate(siens) if e.get("verdict") == "ko"]
        apres = siens[(ko[-1] + 1) if ko else 0:]
        etapes = {}
        for evenement in apres:
            if evenement.get("etape") in ETAPES_REQUISES:
                etapes[evenement["etape"]] = evenement.get("verdict")
        manquantes = [etape for etape in ETAPES_REQUISES if etapes.get(etape) != "ok"]
        if manquantes:
            raise PreuveManquante("%s : preuve(s) manquante(s) ou en echec : %s"
                                  % (hote, ", ".join(manquantes)))
        sante = next((e.get("detail") for e in reversed(apres)
                      if e.get("etape") == "sante"), None)
        resultat[hote] = {"actif": actif, "etapes": {e: etapes[e] for e in ETAPES_REQUISES},
                          "sante": sante}
    return resultat


def ecrire_attestation(chemin: Path, attestation: dict) -> None:
    """Ecrit l'attestation en place (tmp + remplacement), lisible du seul
    proprietaire."""
    chemin.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = chemin.with_name(chemin.name + ".tmp")
    tmp.write_text(json.dumps(attestation, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, chemin)


def envoyer(dest: str, corps: str, charge: dict) -> int:
    """Un courrier urgent (reveil immediat, sans fenetre de regroupement) et
    signale comme evenement de staging. Une seule voie d'envoi, comme le
    courrier d'echec de staging.sh."""
    conn = db_mod.connect(config.load())
    try:
        return mail.send(conn, work.SYSTEM_SENDER, dest, corps, kind="event",
                         payload={"urgent": True, "staging_actif": charge})
    finally:
        conn.close()


def corps_courrier(attestation: dict, chemin: Path) -> str:
    lignes = ["Staging actif : commit %s, installe ET actif sur les %d hotes."
              % (attestation["sha"][:9], len(attestation["hotes"])), ""]
    for hote, preuve in sorted(attestation["hotes"].items()):
        detail = " — %s" % preuve["sante"] if preuve.get("sante") else ""
        lignes.append("- %s : actif %s (%s)%s"
                      % (hote, preuve["actif"][:9],
                         ", ".join("%s ok" % e for e in ETAPES_REQUISES), detail))
    if attestation["lots"]:
        lignes += ["", "Lots concernes : %s"
                   % ", ".join("#%s" % lot for lot in attestation["lots"])]
    lignes += ["", "Preuves : journal %s ; attestation %s"
               % (attestation["journal"], chemin),
               "Aucune cloture automatique : reprends ta verification apres "
               "deploiement et clos le lot selon tes regles."]
    return "\n".join(lignes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="attestation staging actif (#1323)")
    parser.add_argument("--journal", required=True, help="journal JSONL de la sequence")
    parser.add_argument("--sha", required=True, help="commit vise par la sequence")
    parser.add_argument("--depuis", required=True,
                        help="debut de l'execution (ISO UTC, %Y-%m-%dT%H:%M:%SZ)")
    parser.add_argument("--hote", action="append", default=[], metavar="NOM=SHA_ACTIF",
                        help="commit relu actif sur l'hote apres la sequence")
    parser.add_argument("--lot", action="append", default=[], help="lot concerne")
    parser.add_argument("--rapporteur", action="append", default=[],
                        help="agent a reveiller (repete)")
    parser.add_argument("--sans-sonde", action="store_true",
                        help="sequence sans sonde de remise reelle : activation non prouvee")
    parser.add_argument("--etat-dir", default=str(ETAT_DEFAUT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    actifs = {}
    for entree in args.hote:
        nom, _, valeur = entree.partition("=")
        if not nom or not valeur:
            parser.error("--hote attend NOM=SHA_ACTIF (recu : %s)" % entree)
        actifs[nom] = valeur
    sha = args.sha.strip().lower()
    if not sha:
        parser.error("--sha vide")
    journal = Path(args.journal)

    try:
        if args.sans_sonde:
            raise PreuveManquante(
                "sante sans sonde de remise reelle : activation non prouvee")
        evenements = lire_journal(journal, args.depuis)
        preuve = preuves(evenements, sha, actifs)
    except PreuveManquante as exc:
        print("signal actif NON emis : %s" % exc, file=sys.stderr)
        return 3

    etat_dir = Path(args.etat_dir)
    chemin = etat_dir / ("actif-%s.json" % sha[:12])
    ancienne = {}
    if chemin.exists():
        try:
            ancienne = json.loads(chemin.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            ancienne = {}
    notifies = dict(ancienne.get("notifies") or {})
    attestation = {
        "schema": SCHEMA,
        "cle": "%s-%dhotes" % (sha[:12], len(HOTES_CANON)),
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sha": sha,
        "sha_court": sha[:9],
        "journal": str(journal),
        "hotes": preuve,
        "lots": sorted(set(args.lot)),
        "rapporteurs": sorted(set(args.rapporteur)),
        "notifies": notifies,
        "cloture": "aucune : verification apres deploiement par le rapporteur",
    }

    echecs = []
    for nom in attestation["rapporteurs"]:
        if nom in notifies:
            continue
        try:
            message = envoyer(nom, corps_courrier(attestation, chemin),
                              {"sha": sha, "hotes": sorted(preuve), "lots": attestation["lots"]})
        except Exception as exc:  # noqa: BLE001 — le signal durable reste ecrit
            echecs.append(nom)
            print("courrier non remis a %s : %s" % (nom, exc), file=sys.stderr)
            continue
        notifies[nom] = {"ts": attestation["date"], "message": message,
                         "lots": attestation["lots"]}

    ecrire_attestation(chemin, attestation)
    if args.json:
        print(json.dumps(attestation, ensure_ascii=False))
    else:
        print("staging actif : %s sur %d hotes (attestation %s)"
              % (sha[:9], len(preuve), chemin))
        if attestation["rapporteurs"]:
            print("rapporteurs : %s" % ", ".join(
                "%s%s" % (nom, "" if nom in notifies else " (non remis)")
                for nom in attestation["rapporteurs"]))
    return 4 if echecs else 0


if __name__ == "__main__":
    sys.exit(main())
