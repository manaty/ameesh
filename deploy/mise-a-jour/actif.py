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
* un hote declare absent (`--absent nom=raison`) est prouve par un evenement
  `absent`/`reporte` portant le commit vise et une raison, et par AUCUNE preuve
  d'activation. Seuls le poste pc-smichea (mise a jour a son retour) et le
  serveur manaty-mesh-1 (aucun executant, sans acces root : bureau #147558)
  sont reportables ; les trois VM d'agents ne le sont jamais. L'attestation
  porte alors les hotes prouves et le champ `reportes` qui nomme chaque hote
  et sa mise a jour a son retour ; un staging complet le remplace plus tard
  pour le meme commit. Un hote VM d'agents absent, une absence non tracee ou
  contredite ne prouvent rien ;
* le detail de `sante` est le rapport JSON de `sante-courrier.py` : verdict
  `ok` et sonde reellement remise (message, destinataire, hote destinataire,
  delai), du bon hote et du bon commit — un detail absent, textuel ou
  `sans-sonde` ne prouve rien ;
* #4126 (decision du pilote, canon §3 et analogie §12) : une sante non
  concluante (trace `non-concluant`, ou rapport `sans-sonde`/`non-concluant`)
  ne retient plus l'attestation. L'hote y figure sans preuve de remise, avec
  la mention « sonde non concluante : <raison> », reportee au staging suivant
  (`non_concluants`). Seul un negatif (tour en echec, courrier non remis, ko)
  la retient ; sans AUCUNE remise reelle, rien n'est prouve (comme
  `--sans-sonde`) ;
* un hote en mode `serveur` sans executant (0 persona locale, comme
  manaty-mesh-1) ne peut pas etre prouve par une remise locale : sa preuve est
  une sonde CROISEE remise a une persona d'un autre hote canonique, dont la
  remise en base est posterieure a son `redemarrer` — elle traverse donc sa
  base (`--mode nom=serveur` ; avis R2 B1). Un hote a executants garde la
  remise locale, et une sante `sans-sonde` ne prouve rien ;
* le message de sonde existe en base, a bien ete remis, par le systeme, a
  cette persona, pour cet hote, ce commit et l'hote destinataire annonce ;
* le commit relu sur l'hote apres la sequence est le commit vise (`--hote
  nom=sha_actif`, comparaison par prefixe : le poste publie un nom court).

Le signal est durable — une attestation JSON sous
`~/.local/state/ameesh/mise-a-jour/actif-<sha>.json` — et borne : un courrier
par rapporteur, jamais deux fois pour le meme commit, meme apres une erreur
d'ecriture survenue apres l'envoi. La cle d'emission est ecrite dans
l'attestation AVANT l'envoi ; une relance recherche le courrier deja depose
portant cette cle avant de renvoyer. La recherche filtre en base sur le
destinataire, l'expediteur systeme et la cle, sans plafond historique. Si la
lecture echoue (base injoignable), la marque d'emission est preservee et
le courrier n'est PAS renvoye automatiquement : le relais operateur prend le
signal (code de sortie 5, distinct d'un echec). Une preuve manquante n'ecrit
ni attestation ni courrier de reussite. Aucun lot n'est clos automatiquement
et aucun sondage n'est arme : le rapporteur reprend sa verification apres
deploiement lui-meme.

#3981 : la meme preuve est aussi deposee dans le journal du mesh
(`ameesh_events`, type `staging.attested`) sous une forme compacte (hotes
actifs, reportes, commit, date, sans le detail de sante ni les marques
d'emission). C'est la seule forme qu'un autre hote — en particulier le poste
qui fait la coupure — peut relire sans ssh : le fichier local reste la preuve
complete et l'etat d'emission, l'evenement la rend partagee. Le depot de
l'evenement echoue sans annuler le deploiement : l'attestation locale est
ecrite, le code de sortie 4 le dit, et une relance reconcilie le courrier deja
depose (cle d'emission). Depuis une VM sans venv ni base (bureau-goal), cet
outil n'est pas lance localement : staging.sh l'execute sur `HOTE_OUTILS`
(#3976) avec le journal recopie.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from ameesh import config, db as db_mod, mail, storage, work

SCHEMA = "ameesh-staging-actif/1"
#: #3981 : type de l'evenement partage dans `ameesh_events` ; le journal est
#: append-only et lisible de tout hote qui a la base, sans ssh ni fichier.
EVENEMENT_TYPE = "staging.attested"
EVENEMENT_PROJET = "ameesh"
#: instants du journal, tel que produits par `date -u +%Y-%m-%dT%H:%M:%SZ`
ISO_UTC = "%Y-%m-%dT%H:%M:%SZ"
#: instant ISO UTC, tel que produit par `date -u +%Y-%m-%dT%H:%M:%SZ`
DEPUIS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
#: VM d'agents requises, jamais reportables (canon §14).
HOTES_AGENTS = ("ameesh-agents-1", "ameesh-agents-2", "ameesh-agents-3")
HOTE_SERVEUR = "manaty-mesh-1"
HOTES_VMS = (*HOTES_AGENTS, HOTE_SERVEUR)
HOTES_CANON = (*HOTES_VMS, "pc-smichea")
#: Seuls le serveur sans executant et le poste peuvent etre reportes.
HOTES_REPORTABLES = (HOTE_SERVEUR, "pc-smichea")
#: etape de trace qui prouve l'absence declaree d'un hote
ETAPE_ABSENCE = "absent"
VERDICT_ABSENCE = "reporte"
#: modes d'hote connus : `serveur` (sans executant) prouve son activation par
#: une sonde croisee, tout autre mode exige une remise locale.
MODES = ("agents", "poste", "serveur")
#: mode par defaut, ferme : sans `--mode`, la remise locale est exigee.
MODE_DEFAUT = "agents"
#: etapes qui, par hote, prouvent l'activation et pas seulement l'installation
ETAPES_REQUISES = ("installer", "redemarrer", "controler", "sante")
#: #4126 : verdicts de sante qui ne concluent pas (ni preuve ni negatif)
NON_CONCLUANT = "non-concluant"
VERDICTS_NON_CONCLUANTS = ("sans-sonde", NON_CONCLUANT)
#: report d'une sonde non concluante, dans l'attestation et le courrier
REPORT_NON_CONCLUANT = "staging suivant"
#: #4616 : mention de l'attestation d'une sequence lancee avec --sans-retour
RETOUR_DESACTIVE = "desactive"
#: etat par defaut des attestations (meme arbre que le journal de staging)
ETAT_DEFAUT = Path.home() / ".local/state/ameesh/mise-a-jour"
#: code de sortie d'une emission ambigue : rien n'a ete renvoye, le relais
#: operateur doit verifier le depot avant toute relance manuelle.
CODE_AMBIGU = 5
#: code de sortie d'un echec (attestation non ecrite, courrier non remis).
CODE_ECHEC = 4


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


def preuve_sonde(hote: str, detail: object, sha: str,
                 mode: str = MODE_DEFAUT) -> dict:
    """Preuve structuree de la sonde, extraite du detail de l'etape `sante`.

    Le detail est le rapport JSON de `sante-courrier.py` : un texte libre, un
    detail absent ou un verdict `sans-sonde` ne prouvent pas la remise reelle.
    Un hote en mode `serveur` a 0 persona locale est prouve par une sonde
    croisee vers un autre hote canonique ; partout ailleurs la remise doit
    etre locale (avis R2 B1).
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
    destinataire_hote = sonde.get("hote_destinataire")
    if not isinstance(message, int) or isinstance(message, bool) or message <= 0:
        raise PreuveManquante("%s : message de sonde illisible" % hote)
    if not isinstance(destinataire, str) or not destinataire.strip():
        raise PreuveManquante("%s : destinataire de sonde illisible" % hote)
    if not isinstance(delai, (int, float)) or isinstance(delai, bool) or delai < 0:
        raise PreuveManquante("%s : delai de sonde illisible" % hote)
    if not isinstance(destinataire_hote, str) or not destinataire_hote.strip():
        raise PreuveManquante("%s : hote du destinataire de sonde illisible" % hote)
    personas = rapport.get("personas")
    if not isinstance(personas, int) or isinstance(personas, bool) or personas < 0:
        raise PreuveManquante("%s : nombre de personas illisible" % hote)
    # un serveur sans executant n'a aucune persona locale : la preuve doit
    # alors traverser un autre hote canonique. Un hote a executants garde la
    # remise locale, meme si le rapport se declare a 0 persona (porte fermee).
    croisee = mode == "serveur" and personas == 0
    if croisee:
        if destinataire_hote == hote:
            raise PreuveManquante(
                "%s : aucune persona locale, sonde non croisee" % hote)
        if destinataire_hote not in HOTES_CANON:
            raise PreuveManquante(
                "%s : sonde croisee vers un hote hors staging (%s)"
                % (hote, destinataire_hote))
    elif destinataire_hote != hote:
        raise PreuveManquante("%s : sonde remise a un autre hote (%s)"
                              % (hote, destinataire_hote))
    return {"message": message, "destinataire": destinataire,
            "delai_s": float(delai), "hote_destinataire": destinataire_hote}


def mention_non_concluante(hote: str, evenement: dict, sha: str) -> str | None:
    """« sonde non concluante : <raison> » pour une sante qui n'a pas conclu,
    None si la sante doit prouver une remise (#4126).

    La trace `non-concluant` de staging.sh porte le rapport en objet
    `rapport` (absent si l'outil n'a pas tourne : la raison est alors le
    texte de la trace) ; une trace `ok` dont le rapport dit `sans-sonde` ou
    `non-concluant` n'a rien prouve non plus. Un rapport d'un autre hote ou
    d'un autre commit reste une preuve etrangere, jamais une mention.
    """
    rapport = evenement.get("rapport")
    if not isinstance(rapport, dict):
        try:
            rapport = json.loads(evenement.get("detail") or "")
        except (TypeError, ValueError):
            rapport = None
        rapport = rapport if isinstance(rapport, dict) else None
    trace_nc = evenement.get("verdict") == NON_CONCLUANT
    if not trace_nc and (rapport is None
                         or rapport.get("verdict") not in VERDICTS_NON_CONCLUANTS):
        return None
    raison = ""
    if rapport is not None:
        if rapport.get("hote") != hote:
            raise PreuveManquante("%s : rapport de sonde d'un autre hote (%r)"
                                  % (hote, rapport.get("hote")))
        if not prefixe_du_commit(sha, str(rapport.get("sha") or "")):
            raise PreuveManquante("%s : rapport de sonde d'un autre commit" % hote)
        sonde = rapport.get("sonde") if isinstance(rapport.get("sonde"), dict) else {}
        raison = str(rapport.get("erreur") or sonde.get("tour")
                     or rapport.get("pourquoi") or "")
        if sonde.get("destinataire"):
            raison = "%s (sonde %s a %s)" % (raison, sonde.get("message"),
                                             sonde["destinataire"])
    if not raison and trace_nc:
        raison = str(evenement.get("detail") or "")
    return "sonde non concluante : %s" % ((raison.strip() or "raison non dite")[:300])


def horodatage(ts: object) -> float | None:
    """Instant ISO UTC du journal en epoch, ou None s'il est illisible."""
    if not isinstance(ts, str):
        return None
    try:
        return datetime.strptime(ts, ISO_UTC).replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def preuve_absence(hote: str, evenements: list[dict], sha: str,
                   raison: str = "") -> str:
    """Raison de l'absence d'un hote, prouvee par la trace, ou PreuveManquante.

    Une absence declaree sans evenement `absent`/`reporte` au commit vise est
    une simple affirmation ; une preuve d'activation du meme hote la contredit.
    Le detail de l'evenement (la trace de staging.sh) fait foi ; la raison de
    la ligne de commande n'est qu'un repli.
    """
    siens = [e for e in evenements if e.get("hote") == hote]
    if not siens:
        raise PreuveManquante("%s : absence declaree mais aucune trace" % hote)
    autres = [e for e in siens if e.get("etape") != ETAPE_ABSENCE]
    if autres:
        raise PreuveManquante(
            "%s : absence declaree mais %d evenement(s) d'activation la contredisent"
            % (hote, len(autres)))
    valides = [e for e in siens
               if e.get("etape") == ETAPE_ABSENCE
               and e.get("verdict") == VERDICT_ABSENCE
               and prefixe_du_commit(sha, str(e.get("sha") or ""))]
    if not valides:
        raise PreuveManquante(
            "%s : absence non prouvee (etape %s et verdict %s attendus)"
            % (hote, ETAPE_ABSENCE, VERDICT_ABSENCE))
    detail = str(valides[-1].get("detail") or "").strip()
    detail = detail or str(raison or "").strip()
    if not detail:
        raise PreuveManquante("%s : absence sans raison" % hote)
    return detail


def preuves(evenements: list[dict], sha: str, actifs: dict[str, str],
            modes: dict[str, str] | None = None,
            absents: dict[str, str] | None = None
            ) -> tuple[dict, dict]:
    """(preuves, reportes) pour la sequence attestee, ou PreuveManquante.

    La comparaison du commit actif est un prefixe dans les deux sens : le
    poste publie un nom court (`src-<7>`), les VM un SHA complet. `modes`
    (nom -> mode, defaut `agents`) decide quelle remise prouve chaque hote.
    `absents` (nom -> raison) declare les hotes reportes : les trois VM d'agents
    restent requises, et chaque absence doit etre prouvee par la trace.
    """
    modes = modes or {}
    absents = dict(absents or {})
    sha = sha.lower()
    inconnus = sorted(h for h in absents if h not in HOTES_CANON)
    if inconnus:
        raise PreuveManquante(
            "hote(s) absent(s) inconnu(s) : %s" % ", ".join(inconnus))
    non_reportables = sorted(h for h in absents if h not in HOTES_REPORTABLES)
    if non_reportables:
        raise PreuveManquante(
            "seuls %s peuvent etre reportes : %s"
            % (", ".join(HOTES_REPORTABLES), ", ".join(non_reportables)))
    contradictoires = sorted(h for h in absents if h in actifs)
    if contradictoires:
        raise PreuveManquante(
            "hote(s) a la fois actif(s) et absent(s) : %s"
            % ", ".join(contradictoires))
    requis = [h for h in HOTES_CANON if h not in absents]
    agents_manquants = [h for h in HOTES_AGENTS if h not in requis]
    if agents_manquants or sorted(actifs) != sorted(requis):
        raise PreuveManquante(
            "sequence partielle (%s) : les trois VM d'agents sont requises"
            % ", ".join(sorted(actifs)))
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
    reportes = {}
    for hote in sorted(absents):
        reportes[hote] = {
            "raison": preuve_absence(hote, evenements, sha, absents[hote]),
            "mise_a_jour": "a son retour au commit actif, jamais en dessous",
        }
    resultat = {}
    for hote in requis:
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
        manquantes = [etape for etape in ETAPES_REQUISES if etapes.get(etape) != "ok"
                      and not (etape == "sante" and etapes.get(etape) == NON_CONCLUANT)]
        if manquantes:
            raise PreuveManquante("%s : preuve(s) manquante(s) ou en echec : %s"
                                  % (hote, ", ".join(manquantes)))
        sante = next(e for e in reversed(apres) if e.get("etape") == "sante")
        detail = sante.get("detail")
        mention = mention_non_concluante(hote, sante, sha)
        if mention is not None:
            # #4126 : ni preuve ni negatif ; l'hote est atteste avec la
            # mention, reportee au staging suivant, sans remise a relire.
            resultat[hote] = {"actif": actif,
                              "etapes": {e: etapes[e] for e in ETAPES_REQUISES},
                              "sante": detail, "sonde": None,
                              "non_concluant": mention,
                              "report": REPORT_NON_CONCLUANT,
                              "redemarrage_ts": None}
            continue
        sonde = preuve_sonde(hote, detail, sha, modes.get(hote, MODE_DEFAUT))
        redemarrages = [e for e in apres
                        if e.get("etape") == "redemarrer" and e.get("verdict") == "ok"]
        redemarrage_ts = redemarrages[-1].get("ts") if redemarrages else None
        if sonde["hote_destinataire"] != hote and not redemarrage_ts:
            raise PreuveManquante(
                "%s : redemarrage non horodate, remise croisee non datable" % hote)
        resultat[hote] = {"actif": actif,
                          "etapes": {e: etapes[e] for e in ETAPES_REQUISES},
                          "sante": detail, "sonde": sonde,
                          "redemarrage_ts": redemarrage_ts}
    # #4126 : une mention ne remplace la preuve que pour un hote ; sans aucune
    # remise reelle, rien ne prouve la chaine (comme --sans-sonde).
    if all(p.get("sonde") is None for p in resultat.values()):
        raise PreuveManquante(
            "aucune sonde concluante (%s) : activation non prouvee"
            % "; ".join("%s : %s" % (h, p["non_concluant"])
                        for h, p in sorted(resultat.items())))
    return resultat, reportes


def controle_message(row: object, hote: str, sonde: dict, sha: str,
                     apres_ts: object = None) -> dict:
    """Le message de sonde existe, a ete remis, pour cet hote et ce commit.

    Pour une sonde croisee (`hote_destinataire` different de l'hote mesure),
    la remise doit etre posterieure au redemarrage `apres_ts` : c'est elle qui
    prouve que la chaine de l'hote fonctionne apres son activation.
    """
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
    if (charge.get("destinataire_hote") or "") != sonde["hote_destinataire"]:
        raise PreuveManquante("sonde %s : hote destinataire inattendu (%r)"
                              % (message, charge.get("destinataire_hote")))
    if sonde["hote_destinataire"] != hote:
        seuil = horodatage(apres_ts)
        if seuil is None:
            raise PreuveManquante("sonde %s : redemarrage de %s non horodate"
                                  % (message, hote))
        livre = row.get("delivered_ts")
        if not isinstance(livre, (int, float)) or isinstance(livre, bool) or livre < seuil:
            raise PreuveManquante(
                "sonde %s : remise a %s anterieure au redemarrage de %s"
                % (message, sonde["hote_destinataire"], hote))
    return sonde


def verifier_remise(conn, hote: str, sonde: dict, sha: str,
                    apres_ts: object = None) -> dict:
    """Relit le message de sonde en base : sans lui, aucune preuve de remise."""
    return controle_message(mail.get(conn, sonde["message"]), hote, sonde, sha,
                            apres_ts)


def verifier_remises(preuve: dict, sha: str) -> None:
    """Relit en base les messages de sonde des hotes prouves."""
    conn = db_mod.connect(config.load())
    try:
        for hote in preuve:
            sonde = preuve[hote]["sonde"]
            if sonde is None:
                continue  # #4126 : sonde non concluante, aucune remise a relire
            apres_ts = (preuve[hote].get("redemarrage_ts")
                        if sonde["hote_destinataire"] != hote else None)
            verifier_remise(conn, hote, sonde, sha, apres_ts)
    finally:
        conn.close()


def emission_cle(sha: str, nom: str) -> str:
    """Cle d'emission stable : une par (commit, rapporteur)."""
    return "%s:%s" % (sha[:12], nom)


def deja_emis(nom: str, cle: str) -> int | None:
    """Id du signal de cette clé, ou None si son absence est prouvée.

    #4180 : la recherche filtre en base sur le destinataire, l'expéditeur
    système et la clé, sans fenêtre sur l'historique. Elle retrouve aussi un
    dépôt ancien après une erreur d'écriture des marques. Toute erreur de
    lecture se propage : l'appelant garde alors l'émission ambiguë sans renvoi.
    """
    conn = db_mod.connect(config.load())
    try:
        return mail.staging_emission(conn, nom, work.SYSTEM_SENDER, cle)
    finally:
        conn.close()


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


def attestation_partagee(attestation: dict) -> dict:
    """La preuve compacte deposee dans le journal du mesh (#3981).

    Le fichier local reste la preuve complete et l'etat d'emission ; l'evenement
    partage ne porte que ce que `release_cutoff` et `staging_pending` relisent
    d'un autre hote : commit, date, hotes actifs, reportes, lots. Le detail de
    sante (rapport de sonde, journal) et les marques d'emission restent locaux.
    """
    hotes = {}
    for hote, preuve in (attestation.get("hotes") or {}).items():
        if not isinstance(preuve, dict):
            continue
        compacte = {"actif": preuve.get("actif")}
        if isinstance(preuve.get("etapes"), dict):
            compacte["etapes"] = preuve["etapes"]
        if preuve.get("non_concluant"):
            compacte["non_concluant"] = preuve["non_concluant"]
        hotes[hote] = compacte
    partage = {
        "schema": attestation.get("schema"),
        "cle": attestation.get("cle"),
        "date": attestation.get("date"),
        "sha": attestation.get("sha"),
        "sha_court": attestation.get("sha_court"),
        "hotes": hotes,
        "reportes": attestation.get("reportes") or {},
        "non_concluants": attestation.get("non_concluants") or {},
        "lots": attestation.get("lots") or [],
        "rapporteurs": attestation.get("rapporteurs") or [],
    }
    if attestation.get("retour_automatique"):
        partage["retour_automatique"] = attestation["retour_automatique"]
    return partage


def enregistrer_attestation(attestation: dict) -> int:
    """Depose la preuve partagee (deja compacte) dans `ameesh_events` ; son id.

    L'evenement est append-only et se relit de tout hote qui a la base : sans
    lui, une attestation ecrite sur `HOTE_OUTILS` (staging lance depuis une VM)
    resterait invisible du poste qui fait la coupure. Un echec de depot ne
    remet pas en cause le deploiement : l'attestation locale est deja ecrite,
    l'appelant rend le code 4 et une relance reconcilie le courrier.
    """
    conn = db_mod.connect(config.load())
    try:
        return storage.of(conn).events.record(
            EVENEMENT_TYPE, "staging:%s" % attestation["sha"],
            EVENEMENT_PROJET, attestation)
    finally:
        conn.close()


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
    reportes = attestation.get("reportes") or {}
    entete = "Staging actif : commit %s, installe ET actif sur %d hotes." % (
        attestation["sha"][:9], len(attestation["hotes"]))
    if reportes:
        entete = "Staging actif : commit %s, installe ET actif sur %d hotes ; %d reporte(s)." % (
            attestation["sha"][:9], len(attestation["hotes"]), len(reportes))
    lignes = [entete, ""]
    if attestation.get("retour_automatique"):
        lignes[1:1] = ["Retour automatique %s (--sans-retour, #4616)."
                       % attestation["retour_automatique"]]
    for hote, preuve in sorted(attestation["hotes"].items()):
        sonde = preuve.get("sonde") or {}
        detail = ""
        if sonde:
            via = ""
            if sonde.get("hote_destinataire") != hote:
                via = " (via %s)" % sonde.get("hote_destinataire")
            detail = " — sonde %s remise a %s en %ss%s" % (
                sonde.get("message"), sonde.get("destinataire"),
                sonde.get("delai_s"), via)
        if preuve.get("non_concluant"):
            detail = " — %s ; reportee au %s" % (
                preuve["non_concluant"], preuve.get("report") or REPORT_NON_CONCLUANT)
        lignes.append("- %s : actif %s (%s)%s"
                      % (hote, preuve["actif"][:9],
                         ", ".join("%s %s" % (e, (preuve.get("etapes") or {}).get(e, "ok"))
                                   for e in ETAPES_REQUISES), detail))
    for hote, detail in sorted(reportes.items()):
        lignes.append("- %s : REPORTE — %s ; mise a jour a son retour, jamais "
                      "en dessous" % (hote, (detail or {}).get("raison") or "absent"))
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
    parser.add_argument("--absent", action="append", default=[], metavar="NOM=RAISON",
                        help="hote reporte (manaty-mesh-1 ou pc-smichea ; une trace %s/%s"
                             " est exigee)" % (ETAPE_ABSENCE, VERDICT_ABSENCE))
    parser.add_argument("--mode", action="append", default=[], metavar="NOM=MODE",
                        help="mode de l'hote (%s ; defaut %s)"
                             % (", ".join(MODES), MODE_DEFAUT))
    parser.add_argument("--lot", action="append", default=[], help="lot concerne")
    parser.add_argument("--rapporteur", action="append", default=[],
                        help="agent a reveiller (repete)")
    parser.add_argument("--sans-sonde", action="store_true",
                        help="sequence sans sonde de remise reelle : activation non prouvee")
    parser.add_argument("--sans-retour", action="store_true",
                        help="sequence sans retour automatique (#4616) : mention dans l'attestation")
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
    absents = {}
    for entree in args.absent:
        nom, _, valeur = entree.partition("=")
        if not nom or not valeur.strip():
            parser.error("--absent attend NOM=RAISON (recu : %s)" % entree)
        absents[nom] = valeur.strip()
    modes = {}
    for entree in args.mode:
        nom, _, valeur = entree.partition("=")
        if not nom or valeur not in MODES:
            parser.error("--mode attend NOM=%s (recu : %s)"
                         % ("|".join(MODES), entree))
        if nom not in actifs:
            parser.error("--mode : hote inconnu « %s » (declarer --hote)" % nom)
        modes[nom] = valeur
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
        preuve, reportes = preuves(evenements, sha, actifs, modes, absents)
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
        "cle": "%s-%dhotes%s" % (sha[:12], len(preuve),
                                 ("-%dreporte" % len(reportes)) if reportes else ""),
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sha": sha,
        "sha_court": sha[:9],
        "journal": str(journal),
        "hotes": preuve,
        "reportes": reportes,
        #: #4126 : hotes attestes sans preuve de remise, sonde a reprendre
        "non_concluants": {h: p["non_concluant"] for h, p in sorted(preuve.items())
                           if p.get("non_concluant")},
        "lots": sorted(set(args.lot)),
        "rapporteurs": sorted(set(args.rapporteur)),
        "notifies": notifies,
        #: emissions en cours : cle ecrite AVANT l'envoi, pour reconcilier
        #: une relance apres une erreur d'ecriture posterieure a l'envoi.
        "emissions": emissions,
        "cloture": "aucune : verification apres deploiement par le rapporteur",
    }
    if args.sans_retour:
        #: #4616 : sequence menee avec --sans-retour (migration non additive)
        attestation["retour_automatique"] = RETOUR_DESACTIVE

    echecs = []
    ambigus = []
    for nom in attestation["rapporteurs"]:
        if nom in notifies:
            continue
        cle = emission_cle(sha, nom)
        # une marque d'emission deja durable signifie qu'une tentative
        # anterieure n'a pas enregistre son resultat : elle est PRESERVEE
        # telle quelle, jamais reinitialisee (avis R1 B3).
        if not emissions.get(nom):
            emissions[nom] = {"cle": cle, "ts": attestation["date"]}
            try:
                # la cle doit etre durable AVANT l'envoi : sinon un arret entre
                # l'envoi et la sauvegarde des marques produirait un doublon.
                ecrire_attestation(chemin, attestation)
            except OSError as exc:
                emissions.pop(nom, None)
                echecs.append(nom)
                print("cle d'emission non ecrite pour %s (aucun envoi) : %s"
                      % (nom, exc), file=sys.stderr)
                continue
        try:
            deja = deja_emis(nom, cle)
        except Exception as exc:  # noqa: BLE001 — sans relecture, absence non prouvee
            # la relecture a echoue (base injoignable, erreur de lecture), donc
            # l'absence du depot n'est pas prouvee. La marque durable reste ambigue et le
            # relais operateur verifie le depot avant toute relance (code 5,
            # libelle « ambigu », comme annonce par la doc et la docstring).
            ambigus.append(nom)
            print("emission ambigue pour %s, aucun renvoi "
                  "(relecture impossible, relais operateur) : %s"
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
                               "reportes": sorted(reportes),
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
        return CODE_ECHEC
    try:
        enregistrer_attestation(attestation_partagee(attestation))
    except Exception as exc:  # noqa: BLE001 — la preuve locale reste ecrite
        # le fichier local est la preuve de ce lancement ; sans l'evenement
        # partage, un autre hote ne peut pas la lire (#3981). Relancer le meme
        # commit reessaie le depot sans renvoyer un courrier deja depose.
        print("attestation non partagee (journal du mesh) : %s" % exc,
              file=sys.stderr)
        return CODE_ECHEC
    if args.json:
        print(json.dumps(attestation, ensure_ascii=False))
    else:
        print("staging actif : %s sur %d hotes%s (attestation %s)"
              % (sha[:9], len(preuve),
                 (" (%d reporte(s))" % len(reportes)) if reportes else "", chemin))
        if attestation["rapporteurs"]:
            print("rapporteurs : %s" % ", ".join(
                "%s%s" % (nom, "" if nom in notifies else " (non remis)")
                for nom in attestation["rapporteurs"]))
    if ambigus:
        return CODE_AMBIGU
    return CODE_ECHEC if echecs else 0


if __name__ == "__main__":
    sys.exit(main())
