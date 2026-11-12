#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Lectures et contrôles du passage d'EP8.3 à Patroni (lot #6861, EP8.4).

Aide d'`adopter.sh` et de `retour-arriere.sh` : ce module ne fait aucun
geste. Il compare, il lit et il dit ce qui bloquerait. Les sous-commandes :

    adoption.py topologie --topologie T --noeuds N
        topologie.json (Patroni, #6870) et noeuds.conf (EP8.3, #6868)
        décrivent la même grappe : mêmes noms, adresses, priorités,
        nofailover et nosync.
    adoption.py hba --patroni-yml P --hba H [--clients FICHIER]
        règles de pg_hba.conf que le pg_hba rendu par Patroni remplacera
        sans les reprendre (le bloc EP8.3 du rôle rewind est attendu), et
        adresses clientes vues dans pg_stat_activity qu'aucune règle
        hostssl du rendu ne couvre.
    adoption.py slots --topologie T --local NOM [--slots FICHIER]
        slots physiques du primaire que Patroni ne connaît pas (il ne garde
        que les slots de ses membres, au même nom que ceux d'EP8.3).
    adoption.py grappe --env E [--url U] [--topologie T]
                       [--membre NOM --etat leader|replica|sync]
        état de la grappe par l'API REST /cluster, avec l'attente d'un
        membre (code 3 tant que l'état n'est pas atteint) ou, avec
        --topologie, la vérification de la cible : un seul leader, les
        autres en flux, un synchrone parmi les agents, le hub jamais
        synchrone.

Code de sortie : 0 conforme, 1 écart bloquant (détaillé sur stdout), 2
lecture impossible, 3 état attendu pas encore atteint.
"""
from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import os
import sys
from pathlib import Path

ICI = Path(__file__).resolve().parent

# Marqueurs du bloc pg_hba posé par deploy/serveur/replication (EP8.3) : ses
# règles (replicator et rewind sur le réseau privé) sont reprises ou
# remplacées par le rendu Patroni, leur disparition est attendue.
HBA_EP83_DEBUT = "# BEGIN ameesh-replication (lot #6868)"
HBA_EP83_FIN = "# END ameesh-replication (lot #6868)"


def _module(nom: str, fichier: str):
    spec = importlib.util.spec_from_file_location(nom, ICI / fichier)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lire_env(chemin) -> dict:
    """KEY=VALUE d'un fichier d'environnement systemd, sans l'exécuter."""
    valeurs = {}
    for ligne in Path(chemin).read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#") or "=" not in ligne:
            continue
        cle, valeur = ligne.split("=", 1)
        valeurs[cle.strip()] = valeur.strip()
    return valeurs


# --- topologie ----------------------------------------------------------------

def lire_noeuds_conf(chemin) -> list:
    noeuds = []
    for ligne in Path(chemin).read_text(encoding="utf-8").splitlines():
        ligne = ligne.split("#", 1)[0].strip()
        if not ligne:
            continue
        champs = ligne.split()
        if len(champs) != 6:
            raise ValueError("noeuds.conf : six champs attendus sur « %s »" % ligne)
        nom, hote, role, sync, nofail, prio = champs
        noeuds.append({"nom": nom, "hote": hote, "role": role,
                       "synchrone": sync == "oui", "nofailover": nofail == "oui",
                       "priorite": int(prio)})
    return noeuds


def comparer_topologie(topologie: dict, noeuds: list) -> list:
    ecarts = []
    patroni = {n["nom"]: n for n in topologie["noeuds"]}
    ep83 = {n["nom"]: n for n in noeuds}
    for nom in sorted(set(patroni) ^ set(ep83)):
        ecarts.append("%s n'est que dans %s" % (
            nom, "topologie.json" if nom in patroni else "noeuds.conf"))
    for nom in sorted(set(patroni) & set(ep83)):
        p, e = patroni[nom], ep83[nom]
        if p["adresse"] != e["hote"]:
            ecarts.append("%s : adresse %s (topologie) contre %s (noeuds.conf)"
                          % (nom, p["adresse"], e["hote"]))
        if bool(p["nofailover"]) != e["nofailover"]:
            ecarts.append("%s : nofailover diffère" % nom)
        if bool(p["nosync"]) == e["synchrone"]:
            ecarts.append("%s : nosync (topologie) et synchrone (noeuds.conf) se contredisent" % nom)
        if int(p["priorite"]) != e["priorite"]:
            ecarts.append("%s : priorité %s (topologie) contre %s (noeuds.conf)"
                          % (nom, p["priorite"], e["priorite"]))
    primaires = [n["nom"] for n in noeuds if n["role"] == "primaire"]
    if primaires != [topologie["primaire_prefere"]]:
        ecarts.append("primaire d'EP8.3 %s, primaire préféré de Patroni %s"
                      % (",".join(primaires) or "aucun", topologie["primaire_prefere"]))
    return ecarts


# --- pg_hba -------------------------------------------------------------------

def _normaliser(regle: str) -> str:
    return " ".join(regle.split())


def regles_hba(texte: str) -> tuple:
    """(règles hors bloc EP8.3, règles du bloc EP8.3), normalisées."""
    hors, bloc, dedans = [], [], False
    for ligne in texte.splitlines():
        brute = ligne.strip()
        if brute == HBA_EP83_DEBUT:
            dedans = True
            continue
        if brute == HBA_EP83_FIN:
            dedans = False
            continue
        brute = brute.split("#", 1)[0].strip()
        if not brute:
            continue
        (bloc if dedans else hors).append(_normaliser(brute))
    return hors, bloc


def hba_rendu(patroni_yml: str) -> list:
    """Règles `postgresql.pg_hba` du patroni.yml rendu (celles qu'il écrit)."""
    try:
        import yaml  # PyYAML : présent avec Patroni, et sur les postes
    except ImportError:
        yaml = None
    texte = Path(patroni_yml).read_text(encoding="utf-8")
    if yaml is not None:
        document = yaml.safe_load(texte) or {}
        regles = ((document.get("postgresql") or {}).get("pg_hba")) or []
        return [_normaliser(str(r)) for r in regles]
    # Repli sans PyYAML : la liste rendue par generer-configuration.py est
    # « postgresql: » puis « pg_hba: » à deux espaces, entrées à quatre.
    regles, section, dans_pg_hba = [], None, False
    for ligne in texte.splitlines():
        if ligne and not ligne.startswith(" "):
            section = ligne.split(":", 1)[0]
            dans_pg_hba = False
        elif section == "postgresql" and ligne.rstrip() == "  pg_hba:":
            dans_pg_hba = True
        elif dans_pg_hba and ligne.startswith("    - "):
            regles.append(_normaliser(ligne[6:]))
        elif dans_pg_hba and ligne.strip():
            dans_pg_hba = False
    return regles


def reseaux_clients(regles: list) -> list:
    """Réseaux des règles TCP qui admettent un rôle client (hors réplication)."""
    reseaux = []
    for regle in regles:
        champs = regle.split()
        if len(champs) < 5 or champs[0] not in ("host", "hostssl"):
            continue
        if champs[1] == "replication":
            continue
        try:
            reseaux.append(ipaddress.ip_network(champs[3], strict=False))
        except ValueError:
            continue
    return reseaux


def controler_hba(patroni_yml: str, hba: str, clients: str | None) -> list:
    rendu = hba_rendu(patroni_yml)
    if not rendu:
        return ["patroni.yml ne rend aucune règle pg_hba : Patroni laisserait le fichier tel quel"]
    hors, _bloc = regles_hba(Path(hba).read_text(encoding="utf-8"))
    ecarts = ["règle perdue : %s" % r for r in hors if r not in rendu]
    if clients:
        reseaux = reseaux_clients(rendu)
        vues = set()
        for ligne in Path(clients).read_text(encoding="utf-8").splitlines():
            adresse = ligne.strip()
            if not adresse or adresse in vues:
                continue
            vues.add(adresse)
            try:
                ip = ipaddress.ip_address(adresse.split("/", 1)[0])
            except ValueError:
                continue
            if not any(ip.version == r.version and ip in r for r in reseaux):
                ecarts.append("client connecté non couvert : %s" % ip)
    return ecarts


# --- slots --------------------------------------------------------------------

def slot_de(nom: str) -> str:
    # Même règle que replication/lib.sh slot_de et que Patroni
    # (slot_name_from_member_name : minuscules, non alphanumériques en « _ »).
    return "".join(c if c.isalnum() else "_" for c in nom.lower())


def controler_slots(topologie: dict, local: str, slots: list) -> list:
    attendus = {slot_de(n["nom"]) for n in topologie["noeuds"] if n["nom"] != local}
    return ["slot inconnu de Patroni : %s (il le supprimerait)" % s
            for s in slots if s and s not in attendus]


# --- grappe -------------------------------------------------------------------

def lire_grappe(env: dict, url: str | None):
    """État /cluster ; AMEESH_ADOPTION_LECTURE lit un document /cluster
    enregistré au lieu du réseau (témoins, rejeu d'un relevé)."""
    sante = _module("sante_base", "sante-base.py")
    lecture = env.get("AMEESH_ADOPTION_LECTURE")
    if lecture:
        document = json.loads(Path(lecture).read_text(encoding="utf-8"))
        membres = [{"nom": m.get("name"), "role": m.get("role"),
                    "etat": m.get("state"), "retard": m.get("lag")}
                   for m in document.get("members") or [] if isinstance(m, dict)]
        leaders = [m["nom"] for m in membres if role_court(m["role"]) == "leader"]
        return {"leader": leaders[0] if leaders else None, "membres": membres}, None
    if url:
        env = dict(env, AMEESH_DB_REST_URL=url)
    return sante.lire_patroni(env)


def role_court(role: str | None) -> str:
    # Patroni 4 : leader, sync_standby, quorum_standby, replica, standby_leader.
    if role in ("leader", "master", "primary"):
        return "leader"
    if role in ("sync_standby", "quorum_standby"):
        return "sync"
    return "replica"


def etat_membre(grappe: dict, nom: str) -> str | None:
    for membre in grappe["membres"]:
        if membre["nom"] != nom:
            continue
        role = role_court(membre["role"])
        if role == "leader":
            return "leader" if membre["etat"] == "running" else None
        if membre["etat"] != "streaming":
            return None
        return role
    return None


def verifier_cible(grappe: dict, topologie: dict, lag_max: int) -> tuple:
    ecarts, notes = [], []
    noeuds = {n["nom"]: n for n in topologie["noeuds"]}
    membres = {m["nom"]: m for m in grappe["membres"]}
    leaders = [m["nom"] for m in grappe["membres"] if role_court(m["role"]) == "leader"]
    if len(leaders) != 1:
        ecarts.append("leaders : %s (un seul attendu)" % (",".join(leaders) or "aucun"))
    for nom in sorted(set(noeuds) - set(membres)):
        ecarts.append("%s absent de la grappe" % nom)
    for nom in sorted(set(membres) - set(noeuds)):
        ecarts.append("%s membre inconnu de topologie.json" % nom)
    syncs = []
    for nom, membre in sorted(membres.items()):
        role = role_court(membre["role"])
        if role == "leader":
            if membre["etat"] != "running":
                ecarts.append("%s leader dans l'état %s" % (nom, membre["etat"]))
            continue
        if membre["etat"] != "streaming":
            ecarts.append("%s : %s au lieu de streaming" % (nom, membre["etat"]))
        retard = membre.get("retard")
        if isinstance(retard, (int, float)) and retard > lag_max:
            ecarts.append("%s : retard %d octets (> %d)" % (nom, retard, lag_max))
        elif not isinstance(retard, (int, float)):
            ecarts.append("%s : retard illisible (%r)" % (nom, retard))
        if role == "sync":
            syncs.append(nom)
            if noeuds.get(nom, {}).get("nosync"):
                ecarts.append("%s est nosync mais synchrone" % nom)
    candidats = [n for n, v in noeuds.items() if not v["nosync"] and n not in leaders]
    if candidats and not syncs:
        ecarts.append("aucun synchrone parmi %s" % ",".join(candidats))
    if leaders and leaders[0] != topologie["primaire_prefere"]:
        notes.append("leader %s, primaire préféré %s : bascule planifiée "
                     "(patronictl switchover) quand la fenêtre le permet"
                     % (leaders[0], topologie["primaire_prefere"]))
    return ecarts, notes


# --- entrée -------------------------------------------------------------------

def _topologie(chemin):
    generer = _module("generer_configuration", "generer-configuration.py")
    try:
        return generer.charger_topologie(chemin)
    except generer.Erreur as exc:
        raise ValueError(str(exc)) from None


def _sortir(ecarts: list, ok: str) -> int:
    for ecart in ecarts:
        print(ecart)
    if not ecarts:
        print(ok)
    return 1 if ecarts else 0


def main(argv=None) -> int:
    analyseur = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sous = analyseur.add_subparsers(dest="commande", required=True)
    t = sous.add_parser("topologie")
    t.add_argument("--topologie", required=True)
    t.add_argument("--noeuds", required=True)
    h = sous.add_parser("hba")
    h.add_argument("--patroni-yml", required=True)
    h.add_argument("--hba", required=True)
    h.add_argument("--clients")
    s = sous.add_parser("slots")
    s.add_argument("--topologie", required=True)
    s.add_argument("--local", required=True)
    s.add_argument("--slots", help="un nom de slot par ligne (défaut : stdin)")
    g = sous.add_parser("grappe")
    g.add_argument("--env", required=True)
    g.add_argument("--url")
    g.add_argument("--topologie")
    g.add_argument("--lag-max", type=int, default=1048576)
    g.add_argument("--membre")
    g.add_argument("--etat", choices=("leader", "replica", "sync", "flux"))
    g.add_argument("--json", action="store_true")
    args = analyseur.parse_args(argv)
    try:
        if args.commande == "topologie":
            return _sortir(comparer_topologie(_topologie(args.topologie),
                                              lire_noeuds_conf(args.noeuds)),
                           "topologie.json et noeuds.conf concordent")
        if args.commande == "hba":
            return _sortir(controler_hba(args.patroni_yml, args.hba, args.clients),
                           "pg_hba : aucune règle ni client perdu")
        if args.commande == "slots":
            texte = Path(args.slots).read_text(encoding="utf-8") if args.slots \
                else sys.stdin.read()
            return _sortir(controler_slots(_topologie(args.topologie), args.local,
                                           [l.strip() for l in texte.splitlines()]),
                           "slots : tous connus de Patroni")
        env = dict(os.environ)
        env.update(lire_env(args.env))
        grappe, erreur = lire_grappe(env, args.url)
    except (OSError, ValueError, KeyError) as exc:
        print("adoption : %s" % exc, file=sys.stderr)
        return 2
    if erreur:
        print("adoption : %s" % erreur, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(grappe, ensure_ascii=False, sort_keys=True))
    else:
        for membre in grappe["membres"]:
            print("%-18s %-13s %-10s retard=%s" % (membre["nom"], membre["role"],
                                                   membre["etat"], membre["retard"]))
    if args.membre:
        etat = etat_membre(grappe, args.membre)
        attendu = args.etat or "flux"
        if attendu == "flux":
            atteint = etat in ("replica", "sync")
        else:
            atteint = etat == attendu
        return 0 if atteint else 3
    if args.topologie:
        try:
            topologie = _topologie(args.topologie)
        except (OSError, ValueError, KeyError) as exc:
            print("adoption : %s" % exc, file=sys.stderr)
            return 2
        ecarts, notes = verifier_cible(grappe, topologie, args.lag_max)
        for note in notes:
            print("note : %s" % note)
        return _sortir(ecarts, "grappe conforme : un leader, %d en flux"
                       % (len(grappe["membres"]) - 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
