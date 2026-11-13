#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Contrôle de santé local de la grappe etcd + Patroni (EP8.4b, lot #6870).

Lit l'API REST de Patroni, `etcdctl endpoint health`, `df` et
`pg_stat_archiver`, puis produit des alertes au format `ameesh-alert/1` :
`db_no_primary`, `db_failover`, `db_replication_lag`, `db_etcd_no_quorum`,
`db_disk_low`, `db_archive_stale`. Les seuils viennent de `patroni.env` et
peuvent être surchargés en ligne de commande.

Le collecteur écrit `/run/ameesh-db/alertes.json` et le journal systemd ;
le passage local d'`ameesh notify` relaie ce fichier sans la base (EP8.5,
#6862, `src/ameesh/db_sante.py`), donc aussi sans primaire et PC éteint.

Mode hors ligne, pour les essais et les tests : `--lecture FICHIER` lit une
observation JSON au lieu de sonder le réseau. Une observation ratée n'est pas
un échec du service : elle produit `db_no_primary` ou `db_etcd_no_quorum`.
Code de sortie : 0 observation faite (même avec alertes), 1 avec `--strict`
et au moins une alerte, 2 lecture invalide.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SCHEMA = "ameesh-db-sante/1"
ALERT_SCHEMA = "ameesh-alert/1"
GIO = 1024 ** 3

SEUILS_DEFAUT = {
    "seuil_retard_octets": 16 * 1024 * 1024,
    "seuil_disque_gio": 5,
    "seuil_disque_pct": 10,
    "seuil_archive_s": 900,
}

RANG = {
    "db_no_primary": 0,
    "db_etcd_no_quorum": 1,
    "db_failover": 2,
    "db_replication_lag": 3,
    "db_archive_stale": 4,
    "db_disk_low": 5,
}


def _entier(valeur, defaut):
    try:
        nombre = int(valeur)
    except (TypeError, ValueError):
        return defaut
    return nombre if nombre > 0 else defaut


def seuils(env, args=None):
    valeurs = {
        "seuil_retard_octets": _entier(
            env.get("AMEESH_DB_SEUIL_RETARD_OCTETS"), SEUILS_DEFAUT["seuil_retard_octets"]),
        "seuil_disque_gio": _entier(
            env.get("AMEESH_DB_SEUIL_DISQUE_GIO"), SEUILS_DEFAUT["seuil_disque_gio"]),
        "seuil_disque_pct": _entier(
            env.get("AMEESH_DB_SEUIL_DISQUE_PCT"), SEUILS_DEFAUT["seuil_disque_pct"]),
        "seuil_archive_s": _entier(
            env.get("AMEESH_DB_SEUIL_ARCHIVE_S"), SEUILS_DEFAUT["seuil_archive_s"]),
    }
    if args is not None:
        for nom, attribut in (("seuil_retard_octets", "seuil_retard_octets"),
                              ("seuil_disque_gio", "seuil_disque_gio"),
                              ("seuil_disque_pct", "seuil_disque_pct"),
                              ("seuil_archive_s", "seuil_archive_s")):
            valeur = getattr(args, attribut, None)
            if valeur is not None:
                valeurs[nom] = _entier(valeur, valeurs[nom])
    valeurs["seuil_disque_pct"] = min(100, max(1, valeurs["seuil_disque_pct"]))
    return valeurs


def alerte(type_, severite, maintenant, valeur, seuil, detail, grappe, **extra):
    cle = "%s:%s" % (type_, grappe)
    suffixe = extra.pop("key_suffix", "")
    if extra.get("member"):
        cle += ":" + str(extra["member"])
    elif suffixe:
        cle += ":" + str(suffixe)
    sortie = {
        "schema": ALERT_SCHEMA,
        "key": cle,
        "type": type_,
        "agent": None,
        "severity": severite,
        "since": round(float(maintenant), 3),
        "value": valeur,
        "threshold": seuil,
        "detail": detail,
    }
    sortie.update(extra)
    return sortie


def _contexte_ssl(env, cert=False):
    ca = env.get("AMEESH_DB_CA")
    contexte = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
    if cert:
        certfile = env.get("AMEESH_DB_ETCD_CERT")
        keyfile = env.get("AMEESH_DB_ETCD_KEY")
        if not certfile or not keyfile:
            raise ValueError("certificat client etcd absent")
        contexte.load_cert_chain(certfile, keyfile)
    return contexte


def lire_patroni(env):
    url = env.get("AMEESH_DB_REST_URL")
    if not url:
        return None, "AMEESH_DB_REST_URL absent"
    utilisateur = env.get("PATRONI_RESTAPI_USERNAME", "")
    mot_de_passe = env.get("PATRONI_RESTAPI_PASSWORD", "")
    entete = "Basic " + base64.b64encode(
        ("%s:%s" % (utilisateur, mot_de_passe)).encode("utf-8")).decode("ascii")
    requete = urllib.request.Request(url.rstrip("/") + "/cluster",
                                     headers={"Authorization": entete})
    try:
        with urllib.request.urlopen(requete, timeout=5,
                                    context=_contexte_ssl(env)) as reponse:
            charge = reponse.read(1024 * 1024)
        donnees = json.loads(charge.decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return None, "API Patroni illisible : %s" % exc
    if not isinstance(donnees, dict):
        return None, "API Patroni : objet JSON attendu"
    leader = donnees.get("leader")
    nom_leader = leader.get("name") if isinstance(leader, dict) else None
    membres = []
    for membre in donnees.get("members") or []:
        if not isinstance(membre, dict):
            continue
        if not nom_leader and membre.get("role") == "leader":
            nom_leader = membre.get("name")
        membres.append({
            "nom": membre.get("name"),
            "role": membre.get("role"),
            "etat": membre.get("state"),
            "retard": membre.get("lag"),
        })
    return {"leader": nom_leader, "membres": membres}, None


def analyser_etcdctl(texte):
    """Lit la sortie de `etcdctl endpoint health --cluster`."""
    sortie = []
    for ligne in (texte or "").splitlines():
        ligne = ligne.strip()
        if " is healthy: " in ligne:
            url, detail = ligne.split(" is healthy: ", 1)
            sortie.append({"url": url.strip(), "sain": True, "detail": detail.strip()})
        elif " is unhealthy: " in ligne:
            url, detail = ligne.split(" is unhealthy: ", 1)
            sortie.append({"url": url.strip(), "sain": False, "detail": detail.strip()})
    return sortie or None


def lire_etcd(env):
    endpoints = env.get("AMEESH_DB_ETCD_ENDPOINTS")
    if not endpoints:
        return None, "AMEESH_DB_ETCD_ENDPOINTS absent"
    etcdctl = env.get("AMEESH_DB_ETCDCTL") or shutil.which("etcdctl")
    if not etcdctl:
        return None, "etcdctl introuvable"
    commande = [etcdctl, "--endpoints", endpoints, "--command-timeout=5s"]
    if env.get("AMEESH_DB_CA"):
        commande += ["--cacert", env["AMEESH_DB_CA"]]
    if env.get("AMEESH_DB_ETCD_CERT"):
        commande += ["--cert", env["AMEESH_DB_ETCD_CERT"]]
    if env.get("AMEESH_DB_ETCD_KEY"):
        commande += ["--key", env["AMEESH_DB_ETCD_KEY"]]
    commande += ["endpoint", "health", "--cluster"]
    try:
        resultat = subprocess.run(commande, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "etcdctl illisible : %s" % exc
    releve = analyser_etcdctl((resultat.stdout or "") + (resultat.stderr or ""))
    if releve is None:
        return None, "etcdctl : sortie incomprise"
    return releve, None


def lire_disque(env):
    chemin = env.get("AMEESH_DB_DATA_DIR") or "/var/lib/postgresql/17/main"
    try:
        stats = os.statvfs(chemin)
    except OSError as exc:
        return None, "disque %s illisible : %s" % (chemin, exc)
    return {
        "chemin": chemin,
        "total": stats.f_blocks * stats.f_frsize,
        "libre": stats.f_bavail * stats.f_frsize,
    }, None


def analyser_archivage(texte):
    """Lit la ligne `archive_mode|last_archived|last_failed` de psql."""
    lignes = [ligne for ligne in (texte or "").splitlines() if ligne.strip()]
    if not lignes:
        return None
    morceaux = lignes[-1].split("|")
    if len(morceaux) != 3:
        return None
    mode = morceaux[0].strip()
    if mode != "on":
        return None

    def horodatage(valeur):
        try:
            return float(valeur)
        except (TypeError, ValueError):
            return None

    return {"dernier_succes": horodatage(morceaux[1]),
            "dernier_echec": horodatage(morceaux[2])}


def lire_archivage(env):
    psql = env.get("AMEESH_DB_PSQL") or shutil.which("psql")
    if not psql:
        return None, "psql introuvable"
    requete = ("SELECT current_setting('archive_mode'), "
               "extract(epoch from last_archived_time), "
               "extract(epoch from last_failed_time) FROM pg_stat_archiver")
    commande = [psql, "-X", "-A", "-t", "-F", "|", "-d", "postgres", "-c", requete]
    try:
        resultat = subprocess.run(commande, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "psql illisible : %s" % exc
    if resultat.returncode != 0:
        return None, "psql : %s" % (resultat.stderr or "échec").strip()
    return analyser_archivage(resultat.stdout), None


def collecter(env):
    lecture = {"grappe": env.get("AMEESH_DB_GRAPPE", "ameesh"),
               "patroni": None, "etcd": None, "disque": None, "archivage": None,
               "erreurs": {}}
    for nom, fonction in (("patroni", lire_patroni), ("etcd", lire_etcd),
                          ("disque", lire_disque), ("archivage", lire_archivage)):
        valeur, erreur = fonction(env)
        lecture[nom] = valeur
        if erreur:
            lecture["erreurs"][nom] = erreur
    return lecture


def charger_etat(chemin):
    if not chemin:
        return {}
    try:
        etat = json.loads(Path(chemin).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return etat if isinstance(etat, dict) else {}


def sauver_etat(chemin, etat):
    if not chemin:
        return None
    cible = Path(chemin)
    try:
        cible.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporaire = tempfile.mkstemp(dir=str(cible.parent), prefix=".sante-", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as flux:
            json.dump(etat, flux, ensure_ascii=False, sort_keys=True)
            flux.write("\n")
        os.chmod(temporaire, 0o600)
        os.replace(temporaire, cible)
    except OSError as exc:
        return "état non écrit (%s)" % exc
    return None


def ecrire_alertes(chemin, grappe, maintenant, alertes):
    if not chemin:
        return None
    cible = Path(chemin)
    document = {"schema": SCHEMA, "grappe": grappe,
                "genere": round(float(maintenant), 3), "alertes": alertes}
    try:
        cible.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        fd, temporaire = tempfile.mkstemp(dir=str(cible.parent), prefix=".alertes-", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as flux:
            json.dump(document, flux, ensure_ascii=False, sort_keys=True)
            flux.write("\n")
        os.chmod(temporaire, 0o644)
        os.replace(temporaire, cible)
    except OSError as exc:
        return "alertes non écrites (%s)" % exc
    return None


def analyser(lecture, etat=None, maintenant=None, seuils_=None):
    """Retourne (alertes, nouvel_etat) à partir d'une observation."""
    maintenant = float(time.time() if maintenant is None else maintenant)
    seuils_ = seuils_ or dict(SEUILS_DEFAUT)
    grappe = lecture.get("grappe", "ameesh") if isinstance(lecture, dict) else "ameesh"
    etat = dict(etat or {})
    erreurs = (lecture.get("erreurs") or {}) if isinstance(lecture, dict) else {}
    alertes = []

    patroni = lecture.get("patroni") if isinstance(lecture, dict) else None
    membres = patroni.get("membres") if isinstance(patroni, dict) else None
    membres = membres if isinstance(membres, list) else []
    leader = patroni.get("leader") if isinstance(patroni, dict) else None
    if not isinstance(patroni, dict) or not leader:
        detail = erreurs.get("patroni") or "aucun membre leader dans /cluster"
        alertes.append(alerte("db_no_primary", "critical", maintenant, len(membres),
                              1, detail, grappe))
    else:
        ancien = etat.get("leader")
        if ancien and ancien != leader:
            alertes.append(alerte("db_failover", "warning", maintenant, leader, None,
                                  "leader changé de %s à %s" % (ancien, leader), grappe,
                                  previous=ancien))
        for membre in membres:
            if not isinstance(membre, dict) or membre.get("nom") == leader:
                continue
            try:
                retard = int(membre.get("retard"))
            except (TypeError, ValueError):
                continue
            if retard > seuils_["seuil_retard_octets"]:
                alertes.append(alerte(
                    "db_replication_lag", "warning", maintenant, retard,
                    seuils_["seuil_retard_octets"],
                    "%s : retard de %d octets (seuil %d)" % (
                        membre.get("nom"), retard, seuils_["seuil_retard_octets"]),
                    grappe, member=membre.get("nom")))

    etcd = lecture.get("etcd") if isinstance(lecture, dict) else None
    if isinstance(etcd, list) and etcd:
        sains = [membre for membre in etcd if membre.get("sain")]
        if len(sains) < 2:
            mauvais = [membre.get("url") for membre in etcd if not membre.get("sain")]
            alertes.append(alerte(
                "db_etcd_no_quorum", "critical", maintenant, len(sains), 2,
                "%d/%d membres etcd sains ; mauvais : %s" % (
                    len(sains), len(etcd), ", ".join(str(u) for u in mauvais) or "aucun"),
                grappe))
    else:
        detail = erreurs.get("etcd") or "aucune mesure etcd"
        alertes.append(alerte("db_etcd_no_quorum", "critical", maintenant, 0, 2,
                              detail, grappe))

    disque = lecture.get("disque") if isinstance(lecture, dict) else None
    if isinstance(disque, dict):
        total, libre = disque.get("total"), disque.get("libre")
        if isinstance(total, (int, float)) and isinstance(libre, (int, float)) and total > 0:
            pourcent = 100.0 * libre / total
            if libre < seuils_["seuil_disque_gio"] * GIO or pourcent < seuils_["seuil_disque_pct"]:
                alertes.append(alerte(
                    "db_disk_low", "critical", maintenant, libre,
                    seuils_["seuil_disque_gio"] * GIO,
                    "%s : %.1f Gio libres (%.1f %%), seuil %d Gio / %d %%" % (
                        disque.get("chemin"), libre / GIO, pourcent,
                        seuils_["seuil_disque_gio"], seuils_["seuil_disque_pct"]),
                    grappe, percent=round(pourcent, 1)))

    archivage = lecture.get("archivage") if isinstance(lecture, dict) else None
    if isinstance(archivage, dict):
        succes = archivage.get("dernier_succes")
        echec = archivage.get("dernier_echec")
        age = None if succes is None else maintenant - succes
        if age is None or age > seuils_["seuil_archive_s"]:
            detail = ("dernier WAL archivé il y a %d s (seuil %d)" % (age, seuils_["seuil_archive_s"])
                      if age is not None else "aucun WAL archivé")
            alertes.append(alerte("db_archive_stale", "warning", maintenant, age,
                                  seuils_["seuil_archive_s"], detail, grappe))
        if echec is not None and (succes is None or echec > succes):
            alertes.append(alerte("db_archive_stale", "warning", maintenant, age,
                                  seuils_["seuil_archive_s"],
                                  "dernier archivage en échec à %.0f" % echec, grappe,
                                  failed_at=echec, key_suffix="echec"))

    nouvel_etat = dict(etat)
    if leader:
        nouvel_etat["leader"] = leader
    if any(a["type"] == "db_failover" for a in alertes):
        nouvel_etat["dernier_failover"] = maintenant
    nouvel_etat["horodatage"] = maintenant
    alertes.sort(key=lambda a: RANG.get(a["type"], 99))
    return alertes, nouvel_etat


def main(argv=None) -> int:
    analyseur = argparse.ArgumentParser(
        description="Contrôle de santé local de la grappe etcd + Patroni.")
    analyseur.add_argument("--lecture", help="observation JSON hors ligne (sinon sondes réelles)")
    analyseur.add_argument("--etat", default=os.environ.get("AMEESH_DB_ETAT"),
                           help="fichier d'état (défaut : AMEESH_DB_ETAT)")
    analyseur.add_argument("--maintenant", type=float, help="horodatage fixe (tests)")
    analyseur.add_argument("--json", action="store_true", help="sortie JSON")
    analyseur.add_argument("--strict", action="store_true",
                           help="code 1 si au moins une alerte")
    analyseur.add_argument("--alerte-fichier", help="écrire les alertes dans ce fichier")
    analyseur.add_argument("--seuil-retard-octets", type=int)
    analyseur.add_argument("--seuil-disque-gio", type=int)
    analyseur.add_argument("--seuil-disque-pct", type=int)
    analyseur.add_argument("--seuil-archive-s", type=int)
    args = analyseur.parse_args(argv)
    env = dict(os.environ)
    if args.lecture:
        try:
            lecture = json.loads(Path(args.lecture).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print("sante-base : lecture illisible : %s" % exc, file=sys.stderr)
            return 2
        if not isinstance(lecture, dict):
            print("sante-base : lecture : objet JSON attendu", file=sys.stderr)
            return 2
        lecture.setdefault("grappe", env.get("AMEESH_DB_GRAPPE", "ameesh"))
    else:
        lecture = collecter(env)
    maintenant = args.maintenant if args.maintenant is not None else time.time()
    etat = charger_etat(args.etat)
    alertes, nouvel_etat = analyser(lecture, etat, maintenant, seuils(env, args))
    avertissements = []
    for erreur in (sauver_etat(args.etat, nouvel_etat),
                   ecrire_alertes(args.alerte_fichier, lecture.get("grappe", "ameesh"),
                                  maintenant, alertes)):
        if erreur:
            avertissements.append(erreur)
    document = {
        "schema": SCHEMA,
        "ok": True,
        "grappe": lecture.get("grappe", "ameesh"),
        "maintenant": round(float(maintenant), 3),
        "primaire": (lecture.get("patroni") or {}).get("leader")
        if isinstance(lecture.get("patroni"), dict) else None,
        "alertes": alertes,
        "lecture": {cle: lecture.get(cle) for cle in
                    ("patroni", "etcd", "disque", "archivage", "erreurs")},
    }
    if args.json:
        print(json.dumps(document, ensure_ascii=False, sort_keys=True))
    else:
        if alertes:
            for une in alertes:
                print("%(type)s [%(severity)s] %(detail)s" % une)
        else:
            print("grappe saine, primaire %s" % (document["primaire"] or "inconnu"))
        for avertissement in avertissements:
            print("sante-base : %s" % avertissement, file=sys.stderr)
    if args.strict and alertes:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
