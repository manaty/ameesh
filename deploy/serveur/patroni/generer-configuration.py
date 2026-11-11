#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Génère les configurations etcd et Patroni de la grappe ameesh (EP8.4b).

Lot #6870, étude docs/design/etudes/replique-base-mesh.md §4. Le fichier
`topologie.json` est la seule source des noms, adresses, priorités et tags ;
les modèles `modeles/etcd.yml.in` et `modeles/patroni.yml.in` ne contiennent
aucun secret. Les mots de passe viennent de `secrets.env`, produit par
`generer-tls.sh` (0600) et recopié dans le `patroni.env` de chaque nœud, que
Patroni lit par ses variables `PATRONI_*`.

    generer-configuration.py --liste-noeuds
    generer-configuration.py --secrets <secrets.env> --sortie <dossier> --tous
    generer-configuration.py --secrets <secrets.env> --sortie <dossier> \
        --noeud ameesh-agents-1 --verifier

L'option `--verifier` valide hors ligne les modèles avec le schéma de Patroni
(`valider-modeles.py`, même code que `patroni --validate-config`) quand le
module est disponible, puis vérifie les invariants de la grappe. Un module
Patroni absent n'échoue pas : la validation est alors seulement structurelle.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import stat
import string
import subprocess
import sys
import tempfile
from pathlib import Path

ICI = Path(__file__).resolve().parent
MODELE_ETCD = ICI / "modeles" / "etcd.yml.in"
MODELE_PATRONI = ICI / "modeles" / "patroni.yml.in"
VALIDATEUR = ICI / "valider-modeles.py"
TOPOLOGIE_DEFAUT = ICI / "topologie.json"

SECRETS_REQUIS = (
    "PATRONI_RESTAPI_USERNAME",
    "PATRONI_RESTAPI_PASSWORD",
    "PATRONI_REPLICATION_USERNAME",
    "PATRONI_REPLICATION_PASSWORD",
)
CLE_SECRET = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
VALEUR_SECRET = re.compile(r"[A-Za-z0-9._~+-]{1,128}\Z")
NOM_NOEUD = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}\Z")
NOM_GRAPPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")

SEUILS_DEFAUT = {
    "seuil_retard_octets": 16 * 1024 * 1024,
    "seuil_disque_gio": 5,
    "seuil_disque_pct": 10,
    "seuil_archive_s": 900,
}


class Erreur(ValueError):
    """Erreur de configuration, affichée sans jamais révéler un secret."""


def sous(dossier: str, nom: str) -> str:
    return dossier.rstrip("/") + "/" + nom


def charger_topologie(chemin) -> dict:
    try:
        texte = Path(chemin).read_text(encoding="utf-8")
    except OSError as exc:
        raise Erreur("topologie illisible : %s" % exc) from None
    try:
        topologie = json.loads(texte)
    except ValueError as exc:
        raise Erreur("topologie JSON invalide : %s" % exc) from None
    valider_topologie(topologie)
    return topologie


def _entier(valeur, nom: str, minimum: int, maximum: int) -> int:
    if isinstance(valeur, bool) or not isinstance(valeur, int):
        raise Erreur("%s : entier attendu" % nom)
    if not minimum <= valeur <= maximum:
        raise Erreur("%s : hors bornes [%d, %d]" % (nom, minimum, maximum))
    return valeur


def _chemin_absolu(valeur, nom: str) -> str:
    if not isinstance(valeur, str) or not valeur.startswith("/"):
        raise Erreur("%s : chemin absolu attendu" % nom)
    if ".." in Path(valeur).parts or valeur == "/":
        raise Erreur("%s : chemin interdit" % nom)
    return valeur


def valider_topologie(topologie: dict) -> None:
    if not isinstance(topologie, dict):
        raise Erreur("topologie : objet attendu")
    for cle in ("grappe", "namespace", "reseau", "adresse_pc",
                "primaire_prefere", "ports", "chemins", "noeuds"):
        if cle not in topologie:
            raise Erreur("topologie : clé %s manquante" % cle)
    if not isinstance(topologie["grappe"], str) or not NOM_GRAPPE.fullmatch(topologie["grappe"]):
        raise Erreur("grappe : nom invalide")
    namespace = topologie["namespace"]
    if not isinstance(namespace, str) or not namespace.startswith("/") or not namespace.endswith("/"):
        raise Erreur("namespace : /nom/ attendu")
    try:
        reseau = ipaddress.ip_network(topologie["reseau"], strict=False)
    except ValueError:
        raise Erreur("reseau : réseau invalide") from None
    if not reseau.is_private or reseau.prefixlen < 16:
        raise Erreur("reseau : réseau privé d'au moins /16 attendu")
    try:
        adresse_pc = ipaddress.ip_address(topologie["adresse_pc"])
    except ValueError:
        raise Erreur("adresse_pc : adresse invalide") from None
    if not adresse_pc.is_private:
        raise Erreur("adresse_pc : adresse privée attendue")

    ports = topologie["ports"]
    if not isinstance(ports, dict):
        raise Erreur("ports : objet attendu")
    for cle in ("etcd_client", "etcd_peer", "patroni_rest", "postgres"):
        _entier(ports.get(cle), "ports.%s" % cle, 1, 65535)
    if len(set(ports[cle] for cle in ("etcd_client", "etcd_peer", "patroni_rest", "postgres"))) != 4:
        raise Erreur("ports : les quatre ports doivent être distincts")

    chemins = topologie["chemins"]
    if not isinstance(chemins, dict):
        raise Erreur("chemins : objet attendu")
    for cle in ("etcd_conf", "etcd_data", "etcd_tls", "patroni_conf",
                "patroni_tls", "ca", "env", "pg_data", "pg_conf", "pg_bin",
                "pgpass", "etat_sante"):
        _chemin_absolu(chemins.get(cle), "chemins.%s" % cle)

    noeuds = topologie["noeuds"]
    if not isinstance(noeuds, list) or len(noeuds) != 3:
        # Décision owner #207987 a : pas de quatrième votant.
        raise Erreur("noeuds : exactement trois votants attendus")
    noms, adresses = set(), set()
    for indice, noeud in enumerate(noeuds):
        if not isinstance(noeud, dict):
            raise Erreur("noeuds[%d] : objet attendu" % indice)
        nom, adresse = noeud.get("nom"), noeud.get("adresse")
        if not isinstance(nom, str) or not NOM_NOEUD.fullmatch(nom):
            raise Erreur("noeuds[%d] : nom invalide" % indice)
        if nom in noms:
            raise Erreur("noeuds : nom %s en double" % nom)
        noms.add(nom)
        try:
            ip = ipaddress.ip_address(adresse)
        except (TypeError, ValueError):
            raise Erreur("%s : adresse invalide" % nom) from None
        if ip not in reseau or ip in (reseau.network_address, reseau.broadcast_address):
            raise Erreur("%s : adresse hors du réseau privé" % nom)
        if str(ip) in adresses:
            raise Erreur("noeuds : adresse %s en double" % ip)
        adresses.add(str(ip))
        _entier(noeud.get("priorite"), "%s.priorite" % nom, 0, 1000)
        for drapeau in ("nofailover", "nosync"):
            if not isinstance(noeud.get(drapeau), bool):
                raise Erreur("%s.%s : booléen attendu" % (nom, drapeau))

    for noeud in noeuds:
        if noeud["nofailover"]:
            if noeud["priorite"] != 0:
                raise Erreur("%s : nofailover exige une priorité nulle" % noeud["nom"])
        elif noeud["priorite"] == 0:
            raise Erreur("%s : priorité nulle sans nofailover" % noeud["nom"])
        if noeud["nosync"] and not noeud["nofailover"]:
            raise Erreur("%s : nosync exige nofailover (hub uniquement)" % noeud["nom"])

    candidats = [n for n in noeuds if not n["nofailover"]]
    if len(candidats) < 2:
        raise Erreur("noeuds : au moins deux candidats primaires attendus")
    synchrones = [n for n in noeuds if not n["nosync"]]
    if len(synchrones) < 2:
        raise Erreur("noeuds : au moins deux candidats synchrones attendus")
    if len([n for n in noeuds if n["nosync"]]) != 1:
        raise Erreur("noeuds : un seul nœud nosync attendu")

    prefere = topologie["primaire_prefere"]
    if not isinstance(prefere, str) or prefere not in noms:
        raise Erreur("primaire_prefere : nœud inconnu")
    noeud_prefere = next(n for n in noeuds if n["nom"] == prefere)
    if noeud_prefere["nofailover"]:
        raise Erreur("primaire_prefere : le nœud est nofailover")
    meilleure = max(n["priorite"] for n in candidats)
    if noeud_prefere["priorite"] != meilleure:
        raise Erreur("primaire_prefere : priorité maximale attendue")
    if sum(1 for n in candidats if n["priorite"] == meilleure) != 1:
        raise Erreur("primaire_prefere : priorité maximale non unique")

    sante = topologie.get("sante", {})
    if not isinstance(sante, dict):
        raise Erreur("sante : objet attendu")
    bornes = {
        "seuil_retard_octets": (1, 10 ** 12),
        "seuil_disque_gio": (1, 10 ** 6),
        "seuil_disque_pct": (1, 100),
        "seuil_archive_s": (1, 86400),
    }
    for cle, defaut in SEUILS_DEFAUT.items():
        valeur = sante.get(cle, defaut)
        minimum, maximum = bornes[cle]
        _entier(valeur, "sante.%s" % cle, minimum, maximum)
        sante[cle] = valeur
    topologie["sante"] = sante


def charger_secrets(chemin) -> dict:
    if not chemin:
        raise Erreur("secrets absents : lancer d'abord generer-tls.sh")
    try:
        st = os.stat(chemin)
    except OSError as exc:
        raise Erreur("secrets illisibles : %s" % exc) from None
    if not stat.S_ISREG(st.st_mode):
        raise Erreur("secrets : fichier régulier attendu")
    if st.st_mode & 0o007:
        raise Erreur("secrets : permissions trop larges (monde) ; 0600 ou 0640 attendu")
    if st.st_mode & 0o020:
        raise Erreur("secrets : permissions trop larges (groupe inscriptible)")
    if not st.st_mode & 0o400:
        raise Erreur("secrets : lecture du propriétaire requise")
    secrets = {}
    try:
        lignes = Path(chemin).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise Erreur("secrets illisibles : %s" % exc) from None
    for numero, ligne in enumerate(lignes, 1):
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#"):
            continue
        if "=" not in ligne:
            raise Erreur("secrets ligne %d : CLE=VALEUR attendu" % numero)
        cle, valeur = ligne.split("=", 1)
        if not CLE_SECRET.fullmatch(cle) or not VALEUR_SECRET.fullmatch(valeur):
            raise Erreur("secrets ligne %d : nom ou valeur invalide" % numero)
        secrets[cle] = valeur
    for cle in SECRETS_REQUIS:
        if cle not in secrets:
            raise Erreur("secrets : %s manquant" % cle)
    for cle in ("PATRONI_RESTAPI_USERNAME", "PATRONI_REPLICATION_USERNAME"):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}", secrets[cle]):
            raise Erreur("secrets : %s invalide" % cle)
    for cle in ("PATRONI_RESTAPI_PASSWORD", "PATRONI_REPLICATION_PASSWORD"):
        if len(secrets[cle]) < 16:
            raise Erreur("secrets : %s trop court (16 caractères minimum)" % cle)
    return secrets


def rendre(modele: Path, valeurs: dict) -> str:
    try:
        texte = modele.read_text(encoding="utf-8")
    except OSError as exc:
        raise Erreur("modèle illisible %s : %s" % (modele.name, exc)) from None
    try:
        return string.Template(texte).substitute(valeurs)
    except KeyError as exc:
        raise Erreur("modèle %s : variable %s manquante" % (modele.name, exc)) from None
    except ValueError as exc:
        raise Erreur("modèle %s : %s" % (modele.name, exc)) from None


def bloc_pg_hba(topologie: dict) -> str:
    pc = str(ipaddress.ip_address(topologie["adresse_pc"])) + "/32"
    lignes = [
        "local all postgres peer",
        "local all all peer",
        "hostssl replication replicator %s scram-sha-256" % topologie["reseau"],
        "hostssl all all %s scram-sha-256" % topologie["reseau"],
        "hostssl all all %s scram-sha-256" % pc,
    ]
    return "\n".join("    - " + ligne for ligne in lignes)


def bloc_tags(noeud: dict) -> str:
    # Patroni refuse nofailover + failover_priority ensemble : le hub porte le
    # drapeau (priorité effective 0), les agents la priorité de bascule.
    if noeud["nofailover"]:
        lignes = ["nofailover: true"]
        if noeud["nosync"]:
            lignes.append("nosync: true")
    else:
        lignes = ["failover_priority: %d" % noeud["priorite"]]
        if noeud["nosync"]:
            lignes.append("nosync: true")
    return "\n".join("  " + ligne for ligne in lignes)


def ecrire(chemin: Path, texte: str, mode: int) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    try:
        os.chmod(chemin.parent, 0o750)
    except OSError:
        pass
    fd, temporaire = tempfile.mkstemp(dir=str(chemin.parent), prefix=".tmp-", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as flux:
            flux.write(texte)
            flux.flush()
            os.fsync(flux.fileno())
        os.chmod(temporaire, mode)
        os.replace(temporaire, chemin)
    finally:
        if os.path.exists(temporaire):
            os.unlink(temporaire)


def valeurs_communes(topologie: dict, noeud: dict) -> dict:
    chemins, ports = topologie["chemins"], topologie["ports"]
    # Le client etcd du nœud parle d'abord à son membre local, puis aux autres :
    # une panne du réseau privé ne doit pas ajouter un aller-retour inutile.
    ordre = [noeud] + [n for n in topologie["noeuds"] if n["nom"] != noeud["nom"]]
    hotes = ",".join(
        "https://%s:%d" % (n["adresse"], ports["etcd_client"])
        for n in ordre
    )
    initial = ",".join(
        "%s=https://%s:%d" % (n["nom"], n["adresse"], ports["etcd_peer"])
        for n in topologie["noeuds"]
    )
    return {
        "nom": noeud["nom"],
        "adresse": noeud["adresse"],
        "grappe": topologie["grappe"],
        "namespace": topologie["namespace"],
        "etcd_data": chemins["etcd_data"],
        "port_client": ports["etcd_client"],
        "port_peer": ports["etcd_peer"],
        "port_rest": ports["patroni_rest"],
        "port_pg": ports["postgres"],
        "initial_cluster": initial,
        "jeton": topologie["grappe"] + "-etcd",
        "hotes_etcd": hotes,
        "tls_ca": chemins["ca"],
        "tls_serveur_cert": sous(chemins["etcd_tls"], "etcd-server.crt"),
        "tls_serveur_key": sous(chemins["etcd_tls"], "etcd-server.key"),
        "tls_rest_cert": sous(chemins["patroni_tls"], "patroni-rest.crt"),
        "tls_rest_key": sous(chemins["patroni_tls"], "patroni-rest.key"),
        "tls_etcd_client_cert": sous(chemins["patroni_tls"], "etcd-client.crt"),
        "tls_etcd_client_key": sous(chemins["patroni_tls"], "etcd-client.key"),
        "pg_conf": chemins["pg_conf"],
        "pg_data": chemins["pg_data"],
        "pg_bin": chemins["pg_bin"],
        "pgpass": chemins["pgpass"],
        "pg_hba": bloc_pg_hba(topologie),
        "tags": bloc_tags(noeud),
    }


def contenu_patroni_env(topologie: dict, noeud: dict, secrets: dict, hotes: str) -> str:
    chemins, ports, sante = topologie["chemins"], topologie["ports"], topologie["sante"]
    valeurs = [
        "# Généré par generer-configuration.py pour %s ; ne pas éditer." % noeud["nom"],
        "# Secrets de la grappe : 0640 root:postgres, jamais dans le dépôt.",
        "PATRONI_RESTAPI_USERNAME=%s" % secrets["PATRONI_RESTAPI_USERNAME"],
        "PATRONI_RESTAPI_PASSWORD=%s" % secrets["PATRONI_RESTAPI_PASSWORD"],
        "PATRONI_REPLICATION_USERNAME=%s" % secrets["PATRONI_REPLICATION_USERNAME"],
        "PATRONI_REPLICATION_PASSWORD=%s" % secrets["PATRONI_REPLICATION_PASSWORD"],
        "",
        "AMEESH_DB_REST_URL=https://%s:%d" % (noeud["adresse"], ports["patroni_rest"]),
        "AMEESH_DB_ETCD_ENDPOINTS=%s" % hotes,
        "AMEESH_DB_CA=%s" % chemins["ca"],
        "AMEESH_DB_ETCD_CERT=%s" % sous(chemins["patroni_tls"], "etcd-client.crt"),
        "AMEESH_DB_ETCD_KEY=%s" % sous(chemins["patroni_tls"], "etcd-client.key"),
        "AMEESH_DB_DATA_DIR=%s" % chemins["pg_data"],
        "AMEESH_DB_SEUIL_RETARD_OCTETS=%d" % sante["seuil_retard_octets"],
        "AMEESH_DB_SEUIL_DISQUE_GIO=%d" % sante["seuil_disque_gio"],
        "AMEESH_DB_SEUIL_DISQUE_PCT=%d" % sante["seuil_disque_pct"],
        "AMEESH_DB_SEUIL_ARCHIVE_S=%d" % sante["seuil_archive_s"],
        "AMEESH_DB_ETAT=%s" % chemins["etat_sante"],
    ]
    return "\n".join(valeurs) + "\n"


def generer_noeud(topologie: dict, noeud: dict, secrets: dict, sortie: Path) -> dict:
    valeurs = valeurs_communes(topologie, noeud)
    patroni = rendre(MODELE_PATRONI, valeurs)
    etcd = rendre(MODELE_ETCD, valeurs)
    env = contenu_patroni_env(topologie, noeud, secrets, valeurs["hotes_etcd"])
    fichiers = {
        "patroni": sortie / "patroni.yml",
        "etcd": sortie / "etcd.yml",
        "env": sortie / "patroni.env",
    }
    ecrire(fichiers["patroni"], patroni, 0o640)
    ecrire(fichiers["etcd"], etcd, 0o640)
    ecrire(fichiers["env"], env, 0o600)
    return fichiers


def verifier_structure(topologie: dict, secrets: dict, fichiers: dict,
                       noeuds=None) -> None:
    ports = topologie["ports"]
    for noeud in (noeuds if noeuds is not None else topologie["noeuds"]):
        nom = noeud["nom"]
        if nom not in fichiers:
            raise Erreur("vérification : configuration de %s absente" % nom)
        trio = fichiers[nom]
        patroni = trio["patroni"].read_text(encoding="utf-8")
        etcd = trio["etcd"].read_text(encoding="utf-8")
        env = trio["env"].read_text(encoding="utf-8")

        for marqueur in ("synchronous_mode: true", "synchronous_node_count: 1",
                         "synchronous_mode_strict: false", "failsafe_mode: false",
                         "use_pg_rewind: true", "use_slots: true",
                         "maximum_lag_on_failover: 1048576", "ttl: 30",
                         "loop_wait: 10", "retry_timeout: 10"):
            if marqueur not in patroni:
                raise Erreur("%s/patroni.yml : %s manquant" % (nom, marqueur))
        if "name: %s\n" % nom not in patroni:
            raise Erreur("%s/patroni.yml : name incorrect" % nom)
        if "listen: %s:%d\n" % (noeud["adresse"], ports["patroni_rest"]) not in patroni:
            raise Erreur("%s/patroni.yml : REST hors adresse privée" % nom)
        if "connect_address: %s:%d\n" % (noeud["adresse"], ports["postgres"]) not in patroni:
            raise Erreur("%s/patroni.yml : connexion PostgreSQL hors adresse privée" % nom)
        if "0.0.0.0" in patroni or re.search(r"^\s*password\s*:", patroni, re.M):
            raise Erreur("%s/patroni.yml : secret ou écoute publique" % nom)
        for secret in (secrets["PATRONI_RESTAPI_PASSWORD"],
                       secrets["PATRONI_REPLICATION_PASSWORD"]):
            if secret in patroni:
                raise Erreur("%s/patroni.yml : mot de passe présent dans le YAML" % nom)
        if noeud["nofailover"]:
            if "  nofailover: true" not in patroni or "  nosync: true" not in patroni:
                raise Erreur("%s/patroni.yml : tags nofailover/nosync manquants" % nom)
        elif "  failover_priority: %d\n" % noeud["priorite"] not in patroni:
            raise Erreur("%s/patroni.yml : failover_priority manquante" % nom)

        if "name: %s\n" % nom not in etcd:
            raise Erreur("%s/etcd.yml : name incorrect" % nom)
        if "listen-client-urls: https://%s:%d\n" % (noeud["adresse"], ports["etcd_client"]) not in etcd:
            raise Erreur("%s/etcd.yml : écoute client hors adresse privée" % nom)
        if "listen-peer-urls: https://%s:%d\n" % (noeud["adresse"], ports["etcd_peer"]) not in etcd:
            raise Erreur("%s/etcd.yml : écoute pair hors adresse privée" % nom)
        for autre in topologie["noeuds"]:
            if "%s=https://%s:%d" % (autre["nom"], autre["adresse"], ports["etcd_peer"]) not in etcd:
                raise Erreur("%s/etcd.yml : membre %s absent de initial-cluster" % (nom, autre["nom"]))
        if etcd.count("client-cert-auth: true") != 2:
            raise Erreur("%s/etcd.yml : authentification client incomplète" % nom)
        for interdit in ("0.0.0.0", "127.0.0.1", "localhost", "[::]"):
            if interdit in etcd:
                raise Erreur("%s/etcd.yml : écoute interdite (%s)" % (nom, interdit))

        if not env.startswith("# Généré"):
            raise Erreur("%s/patroni.env : en-tête absent" % nom)
        for cle in ("PATRONI_RESTAPI_PASSWORD=", "PATRONI_REPLICATION_PASSWORD=",
                    "AMEESH_DB_REST_URL=https://%s:%d" % (noeud["adresse"], ports["patroni_rest"])):
            if cle not in env:
                raise Erreur("%s/patroni.env : %s manquant" % (nom, cle))
        if stat.S_IMODE(trio["env"].stat().st_mode) & 0o077:
            raise Erreur("%s/patroni.env : permissions trop larges" % nom)

    prefere = topologie["primaire_prefere"]
    meilleure = max(n["priorite"] for n in topologie["noeuds"] if not n["nofailover"])
    gagnant = next(n for n in topologie["noeuds"] if n["priorite"] == meilleure and not n["nofailover"])
    if gagnant["nom"] != prefere:
        raise Erreur("vérification : primaire préféré incohérent")


def lancer_validateur_patroni(fichiers: list) -> bool:
    """Valide les YAML avec Patroni ; True si la validation a eu lieu."""
    if not VALIDATEUR.is_file():
        print("validation Patroni ignorée (valider-modeles.py absent)", file=sys.stderr)
        return False
    candidats = []
    if os.environ.get("PATRONI_PYTHON"):
        candidats.append(os.environ["PATRONI_PYTHON"])
    if Path("/opt/patroni/venv/bin/python").is_file():
        candidats.append("/opt/patroni/venv/bin/python")
    candidats.append(sys.executable)
    python = None
    for candidat in candidats:
        try:
            essai = subprocess.run([candidat, "-c", "import patroni"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if essai.returncode == 0:
            python = candidat
            break
    if python is None:
        print("validation Patroni ignorée (module patroni absent)", file=sys.stderr)
        return False
    commande = [python, str(VALIDATEUR)] + [str(f) for f in fichiers]
    try:
        resultat = subprocess.run(commande, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Erreur("validateur Patroni illisible : %s" % exc) from None
    if resultat.returncode == 3:
        print("validation Patroni ignorée (module patroni absent)", file=sys.stderr)
        return False
    if resultat.returncode != 0:
        detail = (resultat.stdout + resultat.stderr).strip()
        raise Erreur("validation Patroni échouée :\n%s" % detail)
    if resultat.stdout.strip():
        print(resultat.stdout.strip())
    return True


def main(argv=None) -> int:
    analyseur = argparse.ArgumentParser(
        description="Génère les configurations etcd et Patroni de la grappe ameesh.")
    analyseur.add_argument("--topologie", default=str(TOPOLOGIE_DEFAUT),
                           help="fichier de topologie JSON (défaut : topologie.json)")
    analyseur.add_argument("--secrets", help="secrets.env produit par generer-tls.sh")
    analyseur.add_argument("--sortie", help="dossier de sortie")
    analyseur.add_argument("--tous", action="store_true",
                           help="écrire un sous-dossier par nœud (défaut)")
    analyseur.add_argument("--noeud", help="écrire seulement ce nœud, à la racine de --sortie")
    analyseur.add_argument("--verifier", action="store_true",
                           help="valider hors ligne les configurations produites")
    analyseur.add_argument("--liste-noeuds", action="store_true",
                           help="afficher nom<TAB>adresse, sans écrire de fichier")
    args = analyseur.parse_args(argv)
    try:
        topologie = charger_topologie(args.topologie)
        if args.liste_noeuds:
            for noeud in topologie["noeuds"]:
                print("%s\t%s" % (noeud["nom"], noeud["adresse"]))
            return 0
        if not args.sortie:
            raise Erreur("--sortie requis pour générer les configurations")
        if args.noeud and args.tous:
            raise Erreur("--noeud et --tous sont exclusifs")
        secrets = charger_secrets(args.secrets)
        noeuds = topologie["noeuds"]
        if args.noeud:
            noeuds = [n for n in noeuds if n["nom"] == args.noeud]
            if not noeuds:
                raise Erreur("nœud inconnu : %s" % args.noeud)
        sortie = Path(args.sortie)
        sortie.mkdir(parents=True, exist_ok=True, mode=0o750)
        os.chmod(sortie, 0o750)
        fichiers = {}
        for noeud in noeuds:
            dossier = sortie / noeud["nom"] if not args.noeud else sortie
            fichiers[noeud["nom"]] = generer_noeud(topologie, noeud, secrets, dossier)
        if args.verifier:
            verifier_structure(topologie, secrets, fichiers, noeuds=noeuds)
            if not lancer_validateur_patroni([f["patroni"] for f in fichiers.values()]):
                print("validation structurelle seule (Patroni absent)", file=sys.stderr)
        print("configurations écrites : %s" % ", ".join(sorted(fichiers)))
        return 0
    except Erreur as exc:
        print("generer-configuration : %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
