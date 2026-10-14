#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Attestation « staging actif » (#1323).

Appelee par `deploy/mise-a-jour/staging.sh` a la fin d'une sequence staging
reussie (#1155), cette sonde lit le journal JSONL de la sequence et n'ecrit
le signal que si l'ACTIVATION est prouvee, hote par hote :

* `installer`, `redemarrer`, `controler` et `sante` tous « ok » dans la
  fenetre de cette execution (depuis `--depuis`), chaque evenement portant le
  commit vise (un journal d'une autre execution ne prouve rien) ;
* aucun `retour` (rollback) dans cette fenetre ;
* le detail de `sante` est le rapport JSON de `sante-courrier.py` : verdict
  `ok` et sonde reellement remise (message, destinataire, delai), du bon hote
  et du bon commit — un detail absent, textuel ou `sans-sonde` ne prouve rien ;
* le message de sonde existe en base, a bien ete remis, par le systeme, a
  cette persona, pour cet hote et ce commit ;
* le commit relu sur l'hote apres la sequence est le commit vise (`--hote
  nom=sha_actif`, comparaison par prefixe : le poste publie un nom court).

Le signal est durable — une attestation JSON sous
`~/.local/state/ameesh/mise-a-jour/actif-<sha>.json` — et borne : un courrier
par rapporteur, jamais deux fois pour le meme commit, meme apres une erreur
d'ecriture survenue apres l'envoi. La cle d'emission est ecrite dans
l'attestation AVANT l'envoi, et une relance reconcilie l'envoi deja depose en
base avant de renvoyer. Une preuve manquante n'ecrit ni attestation ni
courrier de reussite. Aucun lot n'est clos automatiquement et aucun sondage
n'est arme : le rapporteur reprend sa verification apres deploiement lui-meme.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from ameesh import config, db as db_mod, mail, work

SCHEMA = "ameesh-staging-actif/1"
#: instant ISO UTC, tel que produit par `date -u +%Y-%m-%dT%H:%M:%SZ`
DEPUIS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
#: hôtes du staging complet (canon §14) : un sous-ensemble n'atteste rien
HOTES_CANON = ("ameesh-agents-1", "ameesh-agents-2", "manaty-mesh-1", "pc-smichea")
#: etapes qui, par hote, prouvent l'activation et pas seulement l'installation
ETAPES_REQUISES = ("installer", "redemarrer", "controler", "sante")
#: etat par defaut des attestations (meme arbre que le journal de staging)
ETAT_DEFAUT = Path.home() / ".local/state/ameesh/mise-a-jour"
#: messages relus par rapporteur pour reconcilier un envoi deja depose
HISTORIQUE_RECONCILIATION = 500


class PreuveManquante(Exception):
    """Une preuve d'activation manque : aucun signal de reussite."""


def prefixe_du_commit(sha: str, vu: str) -> bool:
    """`vu` est un prefixe non vide de `sha` (le poste publie un nom court)."""
    sha, vu = (sha or "").lower(), (vu or "").lower()
    return bool(vu) and len(vu) <= len(sha) and sha.startswith(vu)


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


def preuve_sonde(hote: str, detail: object, sha: str) -> dict:
    """Preuve structuree de la sonde, extraite du detail de l'etape `sante`.

    Le detail est le rapport JSON de `sante-courrier.py` : un texte libre, un
    detail absent ou un verdict `sans-sonde` ne prouvent pas la remise reelle.
    """
    if not isinstance(detail, str) or not detail.strip():
        raise PreuveManquante(
            "%s : preuve de sonde absente (detail de sante vide)" % hote)
    try:
        rapport = json.loads(detail)
    except json.JSONDecodeError as exc:
        raise PreuveManquante("%s : preuve de sonde illisible (%s)" % (hote, exc))
    if not isinstance(rapport, dict):
        raise PreuveManquante("%s : preuve de sonde d'une forme inconnue" % hote)
    if rapport.get("hote") != hote:
        raise PreuveManquante("%s : preuve de sonde d'un autre hote (%r)"
                              % (hote, rapport.get("hote")))
    if not prefixe_du_commit(sha, str(rapport.get("sha") or "")):
        raise PreuveManquante("%s : preuve de sonde d'un autre commit" % hote)
    if rapport.get("verdict") != "ok":
        raise PreuveManquante("%s : sonde non remise (verdict %s)"
                              % (hote, rapport.get("verdict") or "absent"))
    sonde = rapport.get("sonde")
    if not isinstance(sonde, dict):
        raise PreuveManquante("%s : aucune sonde de remise reelle" % hote)
    message = sonde.get("message")
    destinataire = sonde.get("destinataire")
    delai = sonde.get("delai_s")
    if not isinstance(message, int) or isinstance(message, bool) or message <= 0:
        raise PreuveManquante("%s : message de sonde illisible" % hote)
    if not isinstance(destinataire, str) or not destinataire.strip():
        raise PreuveManquante("%s : destinataire de sonde illisible" % hote)
    if not isinstance(delai, (int, float)) or isinstance(delai, bool) or delai < 0:
        raise PreuveManquante("%s : delai de sonde illisible" % hote)
    return {"message": message, "destinataire": destinataire, "delai_s": float(delai)}


def preuves(evenements: list[dict], sha: str, actifs: dict[str, str]) -> dict:
    """{hote: preuves} pour la sequence attestee, ou PreuveManquante.

    La comparaison du commit actif est un prefixe dans les deux sens : le
    poste publie un nom court (`src-<7>`), les VM un SHA complet.
    """
    if sorted(actifs) != sorted(HOTES_CANON):
        raise PreuveManquante(
            "sequence partielle (%s) : un staging complet des quatre hotes est requis"
            % ", ".join(sorted(actifs)))
    sha = sha.lower()
    retours = [e for e in evenements if e.get("etape") == "retour"]
    if retours:
        raise PreuveManquante("retour/rollback present dans la fenetre (%d evenement(s))"
                              % len(retours))
    # un evenement de la fenetre sans le commit vise vient d'une autre
    # execution : le journal ne prouve alors rien pour celle-ci (avis R1 B2).
    etrangers = [e for e in evenements
                 if not prefixe_du_commit(sha, str(e.get("sha") or ""))]
    if etrangers:
        raise PreuveManquante(
            "evenement(s) d'une autre execution dans la fenetre (%d) :"
            " journal d'un autre commit" % len(etrangers))
    fins = [e for e in evenements if e.get("etape") == "sequence"]
    if not fins or fins[-1].get("verdict") != "ok":
        raise PreuveManquante("sequence staging non terminee (verdict ok absent)")
    resultat = {}
    for hote in HOTES_CANON:
        actif = (actifs.get(hote) or "").lower()
        if not actif:
            raise PreuveManquante("%s : commit actif illisible" % hote)
        if not prefixe_du_commit(sha, actif):
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
        detail = next((e.get("detail") for e in reversed(apres)
                       if e.get("etape") == "sante"), None)
        resultat[hote] = {"actif": actif, "etapes": {e: etapes[e] for e in ETAPES_REQUISES},
                          "sante": detail, "sonde": preuve_sonde(hote, detail, sha)}
    return resultat


def controle_message(row: object, hote: str, sonde: dict, sha: str) -> dict:
    """Le message de sonde existe, a ete remis, pour cet hote et ce commit."""
    message = sonde["message"]
    if not row:
        raise PreuveManquante("sonde %s introuvable dans le courrier" % message)
    if row.get("sender") != work.SYSTEM_SENDER:
        raise PreuveManquante("sonde %s : expediteur inattendu" % message)
    if row.get("recipient") != sonde["destinataire"]:
        raise PreuveManquante("sonde %s : destine a %r, pas a %r"
                              % (message, row.get("recipient"), sonde["destinataire"]))
    if not row.get("delivered_ts"):
        raise PreuveManquante("sonde %s : message non remis" % message)
    charge = mail.message_payload(row).get("staging") or {}
    if not isinstance(charge, dict) or charge.get("hote") != hote:
        raise PreuveManquante("sonde %s : message d'un autre hote" % message)
    if not prefixe_du_commit(sha, str(charge.get("sha") or "")):
        raise PreuveManquante("sonde %s : message d'un autre commit" % message)
    return sonde


def verifier_remise(conn, hote: str, sonde: dict, sha: str) -> dict:
    """Relit le message de sonde en base : sans lui, aucune preuve de remise."""
    return controle_message(mail.get(conn, sonde["message"]), hote, sonde, sha)


def verifier_remises(preuve: dict, sha: str) -> None:
    """Relit en base les quatre messages de sonde de la sequence."""
    conn = db_mod.connect(config.load())
    try:
        for hote in HOTES_CANON:
            verifier_remise(conn, hote, preuve[hote]["sonde"], sha)
    finally:
        conn.close()


def emission_cle(sha: str, nom: str) -> str:
    """Cle d'emission stable : une par (commit, rapporteur)."""
    return "%s:%s" % (sha[:12], nom)


def deja_emis(nom: str, cle: str) -> int | None:
    """Id du courrier portant cette cle d'emission, s'il a deja ete depose.

    La relecture sert a reconcilier une relance apres une erreur d'ecriture
    survenue entre l'envoi et la sauvegarde des marques : le courrier deja
    depose est retrouve, pas renvoye.
    """
    conn = db_mod.connect(config.load())
    try:
        for row in mail.history(conn, nom, limit=HISTORIQUE_RECONCILIATION):
            if row.get("sender") != work.SYSTEM_SENDER:
                continue
            charge = mail.message_payload(row).get("staging_actif") or {}
            if isinstance(charge, dict) and charge.get("emission") == cle:
                return int(row["id"])
    finally:
        conn.close()
    return None


def ecrire_attestation(chemin: Path, attestation: dict) -> None:
    """Ecrit l'attestation en place (temporaire unique 0600 + remplacement),
    lisible du seul proprietaire."""
    chemin.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    donnees = json.dumps(attestation, ensure_ascii=False, indent=2) + "\n"
    # mkstemp cree le fichier en 0600 des l'ouverture et sous un nom unique :
    # aucune fenetre ou le contenu est lisible par un autre (avis R1 N1).
    fd, temporaire = tempfile.mkstemp(prefix=chemin.name + ".", suffix=".tmp",
                                      dir=str(chemin.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as flux:
            flux.write(donnees)
            flux.flush()
            os.fsync(flux.fileno())
        os.chmod(temporaire, 0o600)
        os.replace(temporaire, chemin)
    except BaseException:
        try:
            os.unlink(temporaire)
        except OSError:
            pass
        raise


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
        sonde = preuve.get("sonde") or {}
        detail = ""
        if sonde:
            detail = " — sonde %s remise a %s en %ss" % (
                sonde.get("message"), sonde.get("destinataire"), sonde.get("delai_s"))
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


def analyseur() -> argparse.ArgumentParser:
    """L'analyseur d'options, construit a part pour que l'aide soit testable
    (un `%` nu dans un help casse `add_argument` a partir de Python 3.13)."""
    parser = argparse.ArgumentParser(description="attestation staging actif (#1323)")
    parser.add_argument("--journal", required=True, help="journal JSONL de la sequence")
    parser.add_argument("--sha", required=True, help="commit vise par la sequence")
    parser.add_argument("--depuis", required=True,
                        help="debut de l'execution (ISO UTC, AAAA-MM-JJTHH:MM:SSZ)")
    parser.add_argument("--hote", action="append", default=[], metavar="NOM=SHA_ACTIF",
                        help="commit relu actif sur l'hote apres la sequence")
    parser.add_argument("--lot", action="append", default=[], help="lot concerne")
    parser.add_argument("--rapporteur", action="append", default=[],
                        help="agent a reveiller (repete)")
    parser.add_argument("--sans-sonde", action="store_true",
                        help="sequence sans sonde de remise reelle : activation non prouvee")
    parser.add_argument("--etat-dir", default=str(ETAT_DEFAUT))
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = analyseur()
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
    if not DEPUIS_RE.match(args.depuis):
        parser.error("--depuis attend un instant ISO UTC (AAAA-MM-JJTHH:MM:SSZ)")
    journal = Path(args.journal)

    try:
        if args.sans_sonde:
            raise PreuveManquante(
                "sante sans sonde de remise reelle : activation non prouvee")
        evenements = lire_journal(journal, args.depuis)
        preuve = preuves(evenements, sha, actifs)
        try:
            verifier_remises(preuve, sha)
        except PreuveManquante:
            raise
        except Exception as exc:  # noqa: BLE001 — la base porte la preuve de remise
            raise PreuveManquante("base du courrier injoignable : %s" % exc)
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
    emissions = dict(ancienne.get("emissions") or {})
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
        #: emissions en cours : cle ecrite AVANT l'envoi, pour reconcilier
        #: une relance apres une erreur d'ecriture posterieure a l'envoi.
        "emissions": emissions,
        "cloture": "aucune : verification apres deploiement par le rapporteur",
    }

    echecs = []
    for nom in attestation["rapporteurs"]:
        if nom in notifies:
            continue
        cle = emission_cle(sha, nom)
        emissions[nom] = {"cle": cle, "ts": attestation["date"]}
        try:
            # la cle doit etre durable AVANT l'envoi : sinon un arret entre
            # l'envoi et la sauvegarde des marques produirait un doublon.
            ecrire_attestation(chemin, attestation)
        except OSError as exc:
            echecs.append(nom)
            print("cle d'emission non ecrite pour %s (aucun envoi) : %s"
                  % (nom, exc), file=sys.stderr)
            continue
        try:
            deja = deja_emis(nom, cle)
        except Exception as exc:  # noqa: BLE001 — sans relecture, pas de renvoi
            echecs.append(nom)
            print("relecture de l'envoi impossible pour %s (aucun renvoi) : %s"
                  % (nom, exc), file=sys.stderr)
            continue
        if deja is not None:
            notifies[nom] = {"ts": attestation["date"], "message": deja,
                             "cle": cle, "reconcilie": True}
            emissions.pop(nom, None)
            continue
        try:
            message = envoyer(nom, corps_courrier(attestation, chemin),
                              {"sha": sha, "hotes": sorted(preuve),
                               "lots": attestation["lots"], "emission": cle})
        except Exception as exc:  # noqa: BLE001 — le signal durable reste ecrit
            echecs.append(nom)
            print("courrier non remis a %s : %s" % (nom, exc), file=sys.stderr)
            continue
        notifies[nom] = {"ts": attestation["date"], "message": message, "cle": cle}
        emissions.pop(nom, None)

    try:
        ecrire_attestation(chemin, attestation)
    except OSError as exc:
        print("attestation non ecrite (%s) : %s" % (chemin, exc), file=sys.stderr)
        return 4
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
