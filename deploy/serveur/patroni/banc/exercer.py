#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Exercice des scénarios EP8.6 sur le banc local de la grappe (lot #6902).

Le banc (``banc.sh``) monte trois conteneurs qui portent chacun etcd, Patroni
et PostgreSQL 17, plus un poste client. Ce script rejoue quatre scénarios,
mesure le délai jusqu'au nouveau primaire, le RPO, l'absence de deux primaires
et le retour de l'ancien primaire, puis écrit un rapport Markdown et JSON au
format prévu pour EP8.6 (``ameesh-banc-grappe/1``).

Sûreté : la cible par défaut est le banc local ; aucune adresse du mesh n'est
utilisée. La cible ``grappe`` refuse de démarrer sans ``--geste-reel`` et sans
actions explicitement configurées dans un fichier de grappe : la préparation ne
contient aucun ordre de geste réel par défaut.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ICI = Path(__file__).resolve().parent
BANC = ICI / "banc.sh"
TOPOLOGIE_BANC = ICI / "topologie-banc.json"
SCHEMA = "ameesh-banc-grappe/1"
LOT = 6902
TABLE_BANC = "banc_jalons"
# Écritures confirmées à accumuler avant un geste : la preuve RPO compare un
# ensemble non trivial, pas les deux premières lignes du journal.
ECRITURES_MINIMUM = 20

TITRES = {
    "perte-primaire": "Perte du primaire (SIGKILL du conteneur)",
    "coupure-reseau": "Coupure réseau du primaire",
    "pc-eteint": "Poste client éteint",
    "deux-noeuds": "Perte de deux nœuds (primaire + standby synchrone)",
}

SCENARIOS = tuple(TITRES)


class Erreur(RuntimeError):
    """Erreur d'exécution, message destiné au rapport."""


class Echec(Erreur):
    """Un critère du scénario n'est pas tenu."""


def maintenant() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    print("[exercer] %s" % message, file=sys.stderr, flush=True)


def run(cmd, *, timeout=30, input=None, check=True, env=None):
    """Exécute une commande bornée ; n'affiche jamais un secret."""
    essai = shlex.join(str(c) for c in cmd)
    try:
        processus = subprocess.run(
            [str(c) for c in cmd],
            input=input,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError as exc:
        raise Erreur("commande absente : %s (%s)" % (cmd[0], exc)) from None
    except subprocess.TimeoutExpired:
        raise Erreur("délai dépassé (%.0f s) : %s" % (timeout, essai)) from None
    if check and processus.returncode != 0:
        detail = (processus.stderr or processus.stdout or "").strip()
        raise Erreur(
            "échec (%d) : %s%s"
            % (processus.returncode, essai, ("\n" + detail[:600]) if detail else "")
        )
    return processus


def lire_json(chemin: Path):
    try:
        return json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Erreur("fichier illisible ou JSON invalide : %s (%s)" % (chemin, exc)) from None


# ---------------------------------------------------------------------------
# Cibles : banc local et grappe réelle (cette dernière seulement sur option).


@dataclass
class SanteEtcd:
    sains: int
    total: int
    detail: str = ""
    lisible: bool = True

    def resume(self) -> str:
        if not self.lisible:
            return "illisible (quorum perdu ?)"
        return "%d/%d sains" % (self.sains, self.total)


class Cible:
    nom = "?"
    reinitialisable = False
    ecrivain_disponible = False
    table_jalon: str | None = None

    def __init__(self, noeuds):
        self.noeuds = noeuds
        self.noeuds_noms = [n["nom"] for n in noeuds]

    # Cycle de vie.
    def demarrer(self) -> None:
        pass

    def arreter(self) -> None:
        pass

    def reinitialiser(self) -> None:
        raise Erreur("réinitialisation non disponible sur cette cible")

    # Gestes.
    def tuer(self, nom: str) -> None:
        raise Erreur("perte de nœud non disponible sur cette cible")

    def demarrer_noeud(self, nom: str) -> None:
        raise Erreur("retour de nœud non disponible sur cette cible")

    def couper(self, nom: str) -> None:
        raise Erreur("coupure réseau non disponible sur cette cible")

    def retablir(self, nom: str) -> None:
        raise Erreur("rétablissement réseau non disponible sur cette cible")

    def arreter_pc(self) -> None:
        raise Erreur("arrêt du poste non disponible sur cette cible")

    def demarrer_pc(self) -> None:
        raise Erreur("démarrage du poste non disponible sur cette cible")

    # Lecture.
    def sql(self, nom: str, requete: str, timeout: float = 5) -> str:
        raise NotImplementedError

    def role(self, nom: str):
        try:
            valeur = self.sql(nom, "SELECT pg_is_in_recovery()", timeout=4).strip()
        except Erreur:
            return None
        if valeur == "t":
            return "t"
        if valeur == "f":
            return "f"
        return None

    def journal(self, nom: str, lignes: int = 400) -> str:
        return ""

    def etcd_sante(self) -> SanteEtcd:
        return SanteEtcd(0, len(self.noeuds), "non disponible", False)

    def versions(self) -> str:
        return ""

    def preparer_jalon(self) -> None:
        if not self.table_jalon:
            raise Echec(
                "aucune table jalon configurée : le RPO ne peut pas être mesuré "
                "sans une table dédiée (option table_jalon de la grappe)"
            )

    def accessibilite(self) -> str:
        return "non vérifiée"


class BancCible(Cible):
    """Banc Docker jetable : tout l'état vit sous $BANC_ETAT."""

    nom = "banc"
    reinitialisable = True
    ecrivain_disponible = True
    table_jalon = TABLE_BANC

    def __init__(self, etat: Path, construire: bool = True):
        super().__init__(lire_json(TOPOLOGIE_BANC)["noeuds"])
        self.etat = Path(etat)
        self.construire = construire
        self.conteneurs: dict[str, str] = {}
        self.projet = ""
        self.pc = ""
        self.monte = False

    def _banc(self, *arguments, timeout=60):
        return run(
            [str(BANC), *arguments],
            timeout=timeout,
            env={**os.environ, "BANC_ETAT": str(self.etat)},
        )

    def _charger(self) -> None:
        etat = lire_json(self.etat / "etat.json")
        self.projet = etat["projet"]
        self.conteneurs = etat["conteneurs"]
        self.pc = etat["conteneur_pc"]
        self.noeuds = etat["topologie"]["noeuds"]
        self.noeuds_noms = [n["nom"] for n in self.noeuds]

    def demarrer(self) -> None:
        etat_json = self.etat / "etat.json"
        if etat_json.exists():
            marqueur = lire_json(etat_json)
            if (
                marqueur.get("banc") != SCHEMA
                or int(marqueur.get("lot") or 0) != LOT
            ):
                raise Erreur(
                    "%s n'est pas un état de banc #%d ; refus de le réutiliser"
                    % (etat_json, LOT)
                )
            self._charger()
            self.monte = True
            return
        arguments = ["monter", "--etat", str(self.etat)]
        if not self.construire:
            arguments.append("--sans-construire")
        self._banc(*arguments, timeout=600)
        self._charger()
        self.monte = True

    def arreter(self) -> None:
        # Rien n'a été monté par cet exercice : ne pas toucher à un dossier
        # --etat préexistant. banc.sh vérifie de toute façon son marqueur.
        if not self.monte:
            return
        self._banc("demonter", timeout=120)

    def _supprimer_repertoire(self) -> None:
        """Le script de banc supprime son état ; ici on ne fait rien."""
        return None

    def reinitialiser(self) -> None:
        self._banc("reinitialiser", timeout=300)
        self._charger()

    def tuer(self, nom: str) -> None:
        run(["docker", "kill", self.conteneurs[nom]], timeout=30)

    def demarrer_noeud(self, nom: str) -> None:
        run(["docker", "start", self.conteneurs[nom]], timeout=30)

    def couper(self, nom: str) -> None:
        self._banc("couper", nom, timeout=30)

    def retablir(self, nom: str) -> None:
        self._banc("retablir", nom, timeout=30)

    def arreter_pc(self) -> None:
        self._banc("arreter-pc", timeout=30)

    def demarrer_pc(self) -> None:
        self._banc("demarrer-pc", timeout=30)

    def sql(self, nom: str, requete: str, timeout: float = 5) -> str:
        conteneur = self.conteneurs[nom]
        processus = run(
            [
                "docker",
                "exec",
                "-u",
                "postgres",
                conteneur,
                "psql",
                "-X",
                "-q",
                "-tA",
                "-F",
                "|",
                "-c",
                requete,
            ],
            timeout=timeout,
        )
        return processus.stdout.strip()

    def journal(self, nom: str, lignes: int = 400) -> str:
        try:
            return self._banc("journal", nom, str(lignes), timeout=30).stdout
        except Erreur:
            return ""

    def _vivant(self, nom: str) -> bool:
        try:
            sortie = run(
                ["docker", "inspect", "-f", "{{.State.Running}}", self.conteneurs[nom]],
                timeout=10,
                check=False,
            )
            return sortie.returncode == 0 and sortie.stdout.strip() == "true"
        except Erreur:
            return False

    def etcd_sante(self) -> SanteEtcd:
        points = ",".join(
            "https://%s:%d" % (n["adresse"], 2379) for n in self.noeuds
        )
        for nom in self.noeuds_noms:
            if not self._vivant(nom):
                continue
            try:
                processus = run(
                    [
                        "docker",
                        "exec",
                        self.conteneurs[nom],
                        "etcdctl",
                        "--endpoints=%s" % points,
                        "--cacert=/etc/ameesh/tls/ca.crt",
                        "--cert=/etc/patroni/tls/etcd-client.crt",
                        "--key=/etc/patroni/tls/etcd-client.key",
                        "endpoint",
                        "health",
                        "--cluster",
                    ],
                    timeout=6,
                    check=False,
                )
            except Erreur:
                continue
            texte = (processus.stdout or "") + (processus.stderr or "")
            if " is healthy" in texte or " is unhealthy" in texte:
                sains = texte.count(" is healthy")
                return SanteEtcd(sains, len(self.noeuds), texte.strip(), True)
        return SanteEtcd(0, len(self.noeuds), "aucun membre lisible", False)

    def versions(self) -> str:
        try:
            return self._banc("versions", timeout=60).stdout.strip()
        except Erreur as exc:
            return "versions indisponibles : %s" % exc

    def accessibilite(self) -> str:
        absents = [n for n in self.conteneurs if not self._vivant(n)]
        return "banc monté" if not absents else "conteneurs arrêtés : %s" % ", ".join(absents)


class GrappeCible(Cible):
    """Grappe réelle : aucune action n'est fournie par le dépôt.

    Le fichier de grappe doit décrire les commandes autorisées ; le script ne
    les invente pas et refuse d'agir si elles manquent. ``--geste-reel`` reste
    obligatoire, même avec des actions configurées.
    """

    nom = "grappe"
    reinitialisable = False
    ecrivain_disponible = False

    ACTIONS = (
        "perte_noeud",
        "retour_noeud",
        "coupure_reseau",
        "retablissement_reseau",
        "arret_pc",
        "demarrage_pc",
    )

    def __init__(self, chemin: Path):
        configuration = lire_json(chemin)
        noeuds = configuration.get("noeuds")
        if not isinstance(noeuds, list) or len(noeuds) != 3:
            raise Erreur("grappe : exactement trois nœuds attendus")
        for noeud in noeuds:
            if not isinstance(noeud, dict) or not isinstance(noeud.get("nom"), str):
                raise Erreur("grappe : nœud invalide")
            noeud.setdefault("hote", noeud["nom"])
            noeud.setdefault("nofailover", False)
        super().__init__(noeuds)
        self.configuration = configuration
        self.actions = configuration.get("actions") or {}
        if not isinstance(self.actions, dict):
            raise Erreur("grappe : actions doit être un objet")
        for action, commande in self.actions.items():
            if action not in self.ACTIONS:
                raise Erreur("grappe : action inconnue %s" % action)
            if not isinstance(commande, str) or not commande.strip():
                raise Erreur("grappe : action %s vide ou non textuelle" % action)
        ssh = configuration.get("ssh") or ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
        if isinstance(ssh, str):
            ssh = shlex.split(ssh)
        self.ssh = list(ssh)
        self.sudo = configuration.get("sudo", "sudo -n")
        self.pc = configuration.get("pc") or {}
        self.table_jalon = configuration.get("table_jalon")
        self.creer_table_jalon = bool(configuration.get("creer_table_jalon", False))
        self.chemins = configuration.get("chemins") or {}

    def _hote(self, nom: str) -> str:
        for noeud in self.noeuds:
            if noeud["nom"] == nom:
                return noeud["hote"]
        raise Erreur("grappe : nœud inconnu %s" % nom)

    def _commande(self, commande: str, nom: str | None = None, timeout: float = 30):
        hote = self.pc.get("hote") if nom is None else self._hote(nom)
        if not hote:
            raise Erreur("grappe : hôte du poste non configuré")
        return run([*self.ssh, hote, commande], timeout=timeout)

    def geste(self, action: str, nom: str | None = None) -> None:
        commande = self.actions.get(action)
        if not commande:
            raise Echec(
                "action %s non configurée : la préparation ne contient aucun geste réel ; "
                "EP8.6 doit la déclarer dans le fichier de grappe après revue" % action
            )
        self._commande(commande, nom=nom, timeout=120)

    def tuer(self, nom: str) -> None:
        self.geste("perte_noeud", nom)

    def demarrer_noeud(self, nom: str) -> None:
        self.geste("retour_noeud", nom)

    def couper(self, nom: str) -> None:
        self.geste("coupure_reseau", nom)

    def retablir(self, nom: str) -> None:
        self.geste("retablissement_reseau", nom)

    def arreter_pc(self) -> None:
        self.geste("arret_pc")

    def demarrer_pc(self) -> None:
        self.geste("demarrage_pc")

    def sql(self, nom: str, requete: str, timeout: float = 8) -> str:
        commande = "%s -u postgres psql -X -q -tA -F '|'" % self.sudo
        processus = run(
            [*self.ssh, self._hote(nom), commande],
            input=requete + "\n",
            timeout=timeout,
        )
        return processus.stdout.strip()

    def journal(self, nom: str, lignes: int = 400) -> str:
        commande = "%s journalctl -u patroni -u etcd --no-pager -n %d" % (self.sudo, lignes)
        try:
            return self._commande(commande, nom=nom, timeout=40).stdout
        except Erreur:
            return ""

    def etcd_sante(self) -> SanteEtcd:
        points = ",".join("https://%s:%d" % (n["adresse"], 2379) for n in self.noeuds)
        ca = self.chemins.get("ca", "/etc/ameesh/tls/ca.crt")
        cert = self.chemins.get("etcd_client_cert", "/etc/patroni/tls/etcd-client.crt")
        cle = self.chemins.get("etcd_client_key", "/etc/patroni/tls/etcd-client.key")
        for nom in self.noeuds_noms:
            commande = (
                "%s etcdctl --endpoints=%s --cacert=%s --cert=%s --key=%s "
                "endpoint health --cluster" % (self.sudo, points, ca, cert, cle)
            )
            try:
                processus = self._commande(commande, nom=nom, timeout=8)
            except Erreur:
                continue
            texte = (processus.stdout or "") + (processus.stderr or "")
            if " is healthy" in texte or " is unhealthy" in texte:
                return SanteEtcd(texte.count(" is healthy"), len(self.noeuds), texte.strip(), True)
        return SanteEtcd(0, len(self.noeuds), "aucun membre lisible", False)

    def preparer_jalon(self) -> None:
        if not self.table_jalon:
            raise Echec(
                "grappe : table_jalon absente du fichier de grappe ; le RPO ne peut pas "
                "être mesuré sans une table dédiée"
            )
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", self.table_jalon):
            raise Erreur("grappe : nom de table jalon invalide")
        if self.creer_table_jalon:
            self.sql(
                self.noeuds[0]["nom"],
                "CREATE TABLE IF NOT EXISTS %s ("
                "id bigserial PRIMARY KEY, scenario text NOT NULL, "
                "cree_le timestamptz NOT NULL DEFAULT clock_timestamp())"
                % self.table_jalon,
                timeout=15,
            )
        else:
            try:
                existe = self.sql(
                    self.noeuds[0]["nom"],
                    "SELECT to_regclass('%s') IS NOT NULL" % self.table_jalon,
                    timeout=10,
                )
            except Erreur as exc:
                raise Echec("grappe : table jalon illisible (%s)" % exc) from None
            if existe.strip() != "t":
                raise Echec(
                    "grappe : table jalon %s absente ; la créer ou autoriser "
                    "creer_table_jalon" % self.table_jalon
                )

    def accessibilite(self) -> str:
        details = []
        for noeud in self.noeuds:
            try:
                valeur = self.sql(noeud["nom"], "SELECT 1", timeout=8)
                details.append("%s=%s" % (noeud["nom"], "ok" if valeur == "1" else "?"))
            except Erreur as exc:
                details.append("%s=injoignable (%s)" % (noeud["nom"], exc))
        return ", ".join(details)


# ---------------------------------------------------------------------------
# Surveillance continue des rôles.


@dataclass
class Echantillon:
    t: float
    roles: dict[str, str]


class Surveillance(threading.Thread):
    """Échantillonne les rôles pour dater la promotion et les deux primaires."""

    def __init__(self, cible: Cible, intervalle: float = 0.5):
        super().__init__(daemon=True)
        self.cible = cible
        self.intervalle = intervalle
        self.echantillons: list[Echantillon] = []
        self._arret = threading.Event()
        self._cond = threading.Condition()

    def run(self) -> None:
        while not self._arret.is_set():
            roles = {nom: self.cible.role(nom) for nom in self.cible.noeuds_noms}
            with self._cond:
                self.echantillons.append(Echantillon(time.time(), roles))
                self._cond.notify_all()
            self._arret.wait(self.intervalle)

    def arreter(self) -> None:
        self._arret.set()
        with self._cond:
            self._cond.notify_all()
        self.join(timeout=10)

    def instantane(self):
        with self._cond:
            return self.echantillons[-1] if self.echantillons else None

    def _boucle(self, predicat, timeout: float):
        debut = time.time()
        consecutif = 0
        while time.time() - debut < timeout:
            with self._cond:
                self._cond.wait(0.25)
                echantillon = self.echantillons[-1] if self.echantillons else None
            if echantillon is None:
                continue
            valeur = predicat(echantillon.roles)
            if valeur:
                consecutif += 1
                if consecutif >= 2:
                    return valeur, time.time() - debut
            else:
                consecutif = 0
        return None, None

    def attendre_nouveau_primaire(self, ancien: str, timeout: float):
        def predicat(roles):
            primaires = [nom for nom, role in roles.items() if role == "f"]
            nouveaux = [nom for nom in primaires if nom != ancien]
            return nouveaux[0] if len(nouveaux) == 1 else None

        return self._boucle(predicat, timeout)

    def attendre_role(self, nom: str, attendu: str, timeout: float):
        def predicat(roles):
            return nom if roles.get(nom) == attendu else None

        return self._boucle(predicat, timeout)

    def primaires_simultanes(self):
        """Durée et bornes des périodes où deux nœuds se disent primaires."""
        periodes = []
        debut = None
        for echantillon in self.echantillons:
            primaires = [nom for nom, role in echantillon.roles.items() if role == "f"]
            if len(primaires) >= 2 and debut is None:
                debut = echantillon.t
            elif len(primaires) < 2 and debut is not None:
                periodes.append((debut, echantillon.t, primaires))
                debut = None
        if debut is not None and self.echantillons:
            periodes.append((debut, self.echantillons[-1].t, []))
        duree = sum(fin - debut for debut, fin, _ in periodes)
        return duree, periodes

    def noeuds_primaires(self) -> list[str]:
        with self._cond:
            if not self.echantillons:
                return []
            return [nom for nom, role in self.echantillons[-1].roles.items() if role == "f"]


# ---------------------------------------------------------------------------
# Lecture des écritures du poste client (banc seulement).


def lire_ecritures(chemin: Path | None) -> list[dict]:
    if chemin is None or not chemin.exists():
        return []
    lignes = []
    try:
        for ligne in chemin.read_text(encoding="utf-8").splitlines():
            try:
                lignes.append(json.loads(ligne))
            except ValueError:
                continue
    except OSError:
        return []
    return lignes


def resume_ecritures(chemin: Path | None) -> dict:
    lignes = lire_ecritures(chemin)
    ids = [int(l["id"]) for l in lignes if isinstance(l.get("id"), int)]
    return {
        "nombre": len(lignes),
        "max_id": max(ids) if ids else 0,
        # Tous les identifiants confirmés par le poste, pas seulement le plus
        # grand : une séquence peut avancer après bascule et laisser un trou.
        "ids": sorted(set(ids)),
        "noeuds": sorted({str(l.get("noeud")) for l in lignes if l.get("noeud")}),
    }


# ---------------------------------------------------------------------------
# Pilote des scénarios.


@dataclass
class Options:
    reinitialiser: bool = True
    duree_pc: int = 20
    # L'exigence de l'étude est 5 min : attendre moins transformerait un
    # dépassement de la cible < 60 s (verdict « limite ») en échec bloqué.
    timeout_promotion: float = 300
    timeout_retour: float = 180
    timeout_sain: float = 180
    delai_quorum: float = 20
    scenarios: tuple[str, ...] = SCENARIOS


class Pilote:
    def __init__(self, cible: Cible, options: Options, etat: Path | None = None):
        self.cible = cible
        self.options = options
        self.etat = Path(etat) if etat else None
        self.ecritures_fichier = (
            self.etat / "client" / "ecritures.jsonl" if self.etat else None
        )
        self.rapport = {
            "schema": SCHEMA,
            "lot": LOT,
            "cible": cible.nom,
            "genere_le": maintenant(),
            "environnement": self._environnement(),
            "constats": self._constats(),
            "scenarios": [],
            "conclusion": {},
        }

    # -- environnement et rapport -----------------------------------------

    def _environnement(self) -> dict:
        environnement = {
            "cible": self.cible.nom,
            "hote": socket.gethostname(),
            "noyau": platform.release(),
            "python": platform.python_version(),
            "date": maintenant(),
            "noeuds": [
                {
                    "nom": noeud["nom"],
                    "adresse": noeud.get("adresse"),
                    "nofailover": bool(noeud.get("nofailover")),
                }
                for noeud in self.cible.noeuds
            ],
            "versions": self.cible.versions(),
            "accessibilite": self.cible.accessibilite(),
        }
        if isinstance(self.cible, BancCible):
            try:
                sortie = run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=15)
                environnement["docker"] = sortie.stdout.strip()
            except Erreur as exc:
                environnement["docker"] = "inconnu (%s)" % exc
        return environnement

    def _constats(self) -> list[dict]:
        if self.etat is None:
            return []
        chemin = self.etat / "constats.json"
        if not chemin.exists():
            return []
        try:
            constats = lire_json(chemin)
        except Erreur:
            return []
        return constats if isinstance(constats, list) else []

    def _resultat(self, nom: str, debut: float, mesures: dict, preuves: dict,
                  notes: list[str], verdict: str) -> dict:
        return {
            "nom": nom,
            "titre": TITRES[nom],
            "debut": datetime.fromtimestamp(debut, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duree_s": round(time.time() - debut, 1),
            "verdict": verdict,
            "mesures": mesures,
            "preuves": preuves,
            "notes": notes,
        }

    def executer(self) -> dict:
        self.cible.preparer_jalon()
        for nom in self.options.scenarios:
            if nom not in TITRES:
                raise Erreur("scénario inconnu : %s" % nom)
            log("scénario %s" % nom)
            debut = time.time()
            try:
                resultat = getattr(self, "_scenario_" + nom.replace("-", "_"))()
                resultat["debut"] = datetime.fromtimestamp(debut, timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                resultat["duree_s"] = round(time.time() - debut, 1)
                resultat["nom"] = nom
                resultat["titre"] = TITRES[nom]
            except (Erreur, OSError) as exc:
                resultat = self._resultat(
                    nom, debut, {}, {}, ["échec : %s" % exc], "blocked"
                )
                resultat["erreur"] = str(exc)
                self.rapport["scenarios"].append(resultat)
                log("scénario %s bloqué : %s" % (nom, exc))
                if not self.cible.reinitialisable:
                    break
                continue
            self.rapport["scenarios"].append(resultat)
            log(
                "scénario %s : %s (%.1f s)"
                % (nom, resultat["verdict"], resultat["duree_s"])
            )
        self.rapport["conclusion"] = self._conclusion()
        return self.rapport

    def _conclusion(self) -> dict:
        verdicts = [s["verdict"] for s in self.rapport["scenarios"]]
        bloques = verdicts.count("blocked")
        limites = verdicts.count("limite")
        if bloques:
            verdict = "blocked"
        elif limites:
            verdict = "limite"
        else:
            verdict = "ok"
        return {
            "verdict": verdict,
            "scenarios_executes": len(verdicts),
            "ok": verdicts.count("ok"),
            "limites": limites,
            "bloques": bloques,
            "resume": "%d scénario(s) exécuté(s) : %d ok, %d limite(s), %d bloqué(s)"
            % (len(verdicts), verdicts.count("ok"), limites, bloques),
        }

    # -- helpers de préparation et de lecture -----------------------------

    def roles(self) -> dict[str, str | None]:
        return {nom: self.cible.role(nom) for nom in self.cible.noeuds_noms}

    def attendre_sain(self, timeout: float | None = None) -> str:
        timeout = timeout if timeout is not None else self.options.timeout_sain
        debut = time.time()
        dernier = None
        while time.time() - debut < timeout:
            roles = self.roles()
            primaires = [nom for nom, role in roles.items() if role == "f"]
            standbys = sum(1 for role in roles.values() if role == "t")
            if len(primaires) == 1 and standbys == 2:
                primaire = primaires[0]
                try:
                    sync = self.cible.sql(primaire, "SHOW synchronous_standby_names")
                except Erreur:
                    sync = ""
                if sync:
                    return primaire
                dernier = "%s sans standby synchrone" % primaire
            else:
                dernier = "primaires=%s, standbys=%d" % (primaires, standbys)
            time.sleep(1)
        raise Echec("grappe non saine après %.0f s (%s)" % (timeout, dernier))

    def attendre_role(self, nom: str, attendu: str, timeout: float) -> float | None:
        debut = time.time()
        while time.time() - debut < timeout:
            if self.cible.role(nom) == attendu:
                return time.time() - debut
            time.sleep(0.5)
        return None

    def preparer(self) -> str:
        if self.cible.reinitialisable and self.options.reinitialiser:
            self.cible.reinitialiser()
        primaire = self.attendre_sain()
        if self.ecritures_fichier:
            self.attendre_ecrivain()
        return primaire

    def attendre_ecrivain(self, timeout: float = 30) -> int:
        if not self.ecritures_fichier:
            return 0
        debut = time.time()
        while time.time() - debut < timeout:
            nombre = resume_ecritures(self.ecritures_fichier)["nombre"]
            if nombre >= ECRITURES_MINIMUM:
                return nombre
            time.sleep(0.5)
        raise Echec(
            "le poste client n'écrit pas assez (fichier %s) après %.0f s"
            % (self.ecritures_fichier, timeout)
        )

    def ecritures(self) -> dict:
        return resume_ecritures(self.ecritures_fichier)

    def inserer_jalon(self, primaire: str, scenario: str) -> int:
        table = self.cible.table_jalon
        if not table:
            raise Echec("aucune table jalon configurée")
        scenario_sql = scenario.replace("'", "''")
        valeur = self.cible.sql(
            primaire,
            "INSERT INTO %s(scenario) VALUES ('%s') RETURNING id"
            % (table, scenario_sql),
            timeout=10,
        )
        try:
            return int(valeur)
        except ValueError:
            raise Echec("jalon non confirmé (réponse %r)" % valeur) from None

    def jalon_present(self, nom: str, jalon: int) -> bool:
        try:
            valeur = self.cible.sql(
                nom,
                "SELECT count(*) FROM %s WHERE id=%d" % (self.cible.table_jalon, jalon),
                timeout=8,
            )
        except Erreur:
            return False
        return valeur.strip() == "1"

    def max_ecriture(self, nom: str) -> int | None:
        if not self.cible.ecrivain_disponible:
            return None
        try:
            return int(
                self.cible.sql(
                    nom, "SELECT COALESCE(max(id),0) FROM banc_ecritures", timeout=8
                )
            )
        except (Erreur, ValueError):
            return None

    def ecritures_absentes(self, nom: str, ids: list[int]) -> list[int]:
        """Identifiants confirmés par le poste qui manquent sur ``nom``.

        On compare l'ensemble exact des identifiants lus dans le journal du
        poste à ceux présents sur le nœud : ``max(id)`` ne suffirait pas, une
        séquence peut avancer après bascule et laisser un trou.
        """
        if not ids:
            return []
        try:
            reponse = self.cible.sql(
                nom,
                "SELECT id FROM banc_ecritures WHERE id IN (%s)"
                % ",".join(str(i) for i in ids),
                timeout=15,
            )
        except Erreur:
            return list(ids)
        presents = {int(v) for v in reponse.splitlines() if v.strip().isdigit()}
        return [i for i in ids if i not in presents]

    def etat_cluster(self) -> dict:
        roles = self.roles()
        primaires = [nom for nom, role in roles.items() if role == "f"]
        return {
            "roles": roles,
            "primaire": primaires[0] if len(primaires) == 1 else None,
            "primaires": primaires,
            "etcd": self.cible.etcd_sante().resume(),
        }

    def diagnostic_promotion(self, perdu: str) -> str:
        """État compact des nœuds survivants quand aucune promotion n'est vue."""
        roles = self.roles()
        etcd = self.cible.etcd_sante()
        journaux = {}
        for nom in self.cible.noeuds_noms:
            if nom == perdu:
                continue
            extrait = _extrait(
                self.cible.journal(nom, 200),
                ("promote", "demot", "error", "failed", "quorum", "leader"),
            )
            if extrait:
                journaux[nom] = " | ".join(extrait[-3:])
        return "rôles=%s ; etcd=%s ; journaux=%s" % (
            roles,
            etcd.resume(),
            journaux or "aucun",
        )

    # -- scénarios ---------------------------------------------------------

    def _scenario_perte_primaire(self) -> dict:
        primaire = self.preparer()
        jalon = self.inserer_jalon(primaire, "perte-primaire")
        avant = self.ecritures()
        surveillance = Surveillance(self.cible)
        perdu = False
        retour_tente = False
        surveillance.start()
        try:
            t0 = time.time()
            self.cible.tuer(primaire)
            perdu = True
            nouveau, delai = surveillance.attendre_nouveau_primaire(
                primaire, self.options.timeout_promotion
            )
            if not nouveau:
                raise Echec(
                    "aucun nouveau primaire après %.0f s (%s)"
                    % (
                        self.options.timeout_promotion,
                        self.diagnostic_promotion(primaire),
                    )
                )
            log("nouveau primaire %s après %.1f s" % (nouveau, delai))
            rpo_jalon = self.jalon_present(nouveau, jalon)
            max_apres = self.max_ecriture(nouveau)
            manquantes = self.ecritures_absentes(nouveau, avant["ids"])
            retour_debut = time.time()
            self.cible.demarrer_noeud(primaire)
            retour_tente = True
            retour, delai_retour = surveillance.attendre_role(
                primaire, "t", self.options.timeout_retour
            )
            fin = self.attendre_sain(timeout=self.options.timeout_sain)
            jalon_final = self.jalon_present(fin, jalon)
            journal = self.cible.journal(primaire, 400)
            split, periodes = surveillance.primaires_simultanes()
        finally:
            if perdu and not retour_tente:
                try:
                    self.cible.demarrer_noeud(primaire)
                except Erreur as exc:
                    log("retour de %s non tenté : %s" % (primaire, exc))
            surveillance.arreter()
        rewind = bool(
            re.search(r"running pg_rewind", journal)
            and re.search(r"pg_rewind exit code=0", journal)
        )
        rpo_ecritures = (
            not manquantes if self.cible.ecrivain_disponible else None
        )
        critere = (
            delai is not None
            and delai <= 300
            and rpo_jalon
            and jalon_final
            and split == 0
            and retour is not None
            and rpo_ecritures is not False
        )
        if not critere:
            verdict = "blocked"
        elif delai > 60:
            verdict = "limite"
        else:
            verdict = "ok"
        note = (
            "Cible < 60 s : %s ; exigence < 300 s : %s."
            % ("tenue" if delai is not None and delai <= 60 else "manquée",
               "tenue" if delai is not None and delai <= 300 else "manquée")
        )
        if rpo_ecritures is False:
            note += (
                " Les écritures confirmées du poste ne sont pas toutes présentes "
                "(%d manquante(s) sur %d)." % (len(manquantes), len(avant["ids"]))
            )
        return self._resultat(
            "perte-primaire",
            time.time() - (delai or 0) - 1,
            {
                "delai_nouveau_primaire_s": None if delai is None else round(delai, 1),
                "cible_60s": bool(delai is not None and delai <= 60),
                "exigence_300s": bool(delai is not None and delai <= 300),
                "retour_ancien_primaire_s": None if delai_retour is None else round(delai_retour, 1),
                "rpo_jalon": rpo_jalon,
                "rpo_ecritures": rpo_ecritures,
                "ecritures_confirmees": len(avant["ids"]),
                "ecritures_absentes": len(manquantes),
                "max_ecriture_avant": avant["max_id"],
                "max_ecriture_apres": max_apres,
                "split_brain_s": round(split, 1),
            },
            {
                "primaire_initial": primaire,
                "nouveau_primaire": nouveau,
                "primaire_final": fin,
                "jalon_id": jalon,
                "jalon_sur_nouveau_primaire": rpo_jalon,
                "jalon_sur_grappe_finale": jalon_final,
                "pg_rewind": rewind,
                "journal_pg_rewind": _extrait(journal, ("pg_rewind", "rewind")),
                "periodes_deux_primaires": len(periodes),
                "ecritures_absentes_ids": manquantes[:20],
            },
            [note],
            verdict,
        )

    def _scenario_coupure_reseau(self) -> dict:
        primaire = self.preparer()
        jalon = self.inserer_jalon(primaire, "coupure-reseau")
        avant = self.ecritures()
        surveillance = Surveillance(self.cible)
        surveillance.start()
        retabli = False
        try:
            t0 = time.time()
            self.cible.couper(primaire)
            nouveau, delai = surveillance.attendre_nouveau_primaire(
                primaire, self.options.timeout_promotion
            )
            if not nouveau:
                raise Echec(
                    "aucun nouveau primaire après %.0f s (%s)"
                    % (
                        self.options.timeout_promotion,
                        self.diagnostic_promotion(primaire),
                    )
                )
            demotion, delai_demotion = surveillance.attendre_role(
                primaire, "t", self.options.timeout_promotion
            )
            rpo_jalon = self.jalon_present(nouveau, jalon)
            max_apres = self.max_ecriture(nouveau)
            manquantes = self.ecritures_absentes(nouveau, avant["ids"])
            self.cible.retablir(primaire)
            retabli = True
            retour, delai_retour = surveillance.attendre_role(
                primaire, "t", self.options.timeout_retour
            )
            fin = self.attendre_sain(timeout=self.options.timeout_sain)
            jalon_final = self.jalon_present(fin, jalon)
            journal = self.cible.journal(primaire, 400)
            split, periodes = surveillance.primaires_simultanes()
        finally:
            if not retabli:
                try:
                    self.cible.retablir(primaire)
                except Erreur:
                    pass
            surveillance.arreter()
        rpo_ecritures = (
            not manquantes if self.cible.ecrivain_disponible else None
        )
        rewind = bool(
            re.search(r"running pg_rewind", journal)
            and re.search(r"pg_rewind exit code=0", journal)
        )
        critere = (
            delai is not None
            and delai <= 300
            and rpo_jalon
            and jalon_final
            and split == 0
            and retour is not None
            and rpo_ecritures is not False
        )
        if not critere:
            verdict = "blocked"
        elif delai > 60:
            verdict = "limite"
        else:
            verdict = "ok"
        notes = []
        if split:
            notes.append(
                "Deux nœuds se sont déclarés primaires pendant %.1f s ; le nœud coupé "
                "n'était joignable par aucun client du banc." % split
            )
        if demotion is None:
            notes.append(
                "L'ancien primaire ne s'est pas démotionné avant la fin de l'observation."
            )
        if delai is not None and delai > 60:
            notes.append("La cible < 60 s est manquée ; l'exigence < 300 s reste tenue.")
        if rpo_ecritures is False:
            notes.append(
                "Les écritures confirmées du poste ne sont pas toutes présentes "
                "(%d manquante(s) sur %d)." % (len(manquantes), len(avant["ids"]))
            )
        return self._resultat(
            "coupure-reseau",
            time.time() - (delai or 0) - 1,
            {
                "delai_nouveau_primaire_s": None if delai is None else round(delai, 1),
                "cible_60s": bool(delai is not None and delai <= 60),
                "exigence_300s": bool(delai is not None and delai <= 300),
                "delai_demotion_ancien_primaire_s": (
                    None if delai_demotion is None else round(delai_demotion, 1)
                ),
                "retour_ancien_primaire_s": None if delai_retour is None else round(delai_retour, 1),
                "rpo_jalon": rpo_jalon,
                "rpo_ecritures": rpo_ecritures,
                "ecritures_confirmees": len(avant["ids"]),
                "ecritures_absentes": len(manquantes),
                "max_ecriture_avant": avant["max_id"],
                "max_ecriture_apres": max_apres,
                "split_brain_s": round(split, 1),
            },
            {
                "primaire_initial": primaire,
                "nouveau_primaire": nouveau,
                "primaire_final": fin,
                "jalon_id": jalon,
                "jalon_sur_nouveau_primaire": rpo_jalon,
                "jalon_sur_grappe_finale": jalon_final,
                "pg_rewind": rewind,
                "journal_pg_rewind": _extrait(journal, ("pg_rewind", "rewind")),
                "periodes_deux_primaires": len(periodes),
                "demotion_observee": demotion is not None,
                "ecritures_absentes_ids": manquantes[:20],
            },
            notes,
            verdict,
        )

    def _scenario_pc_eteint(self) -> dict:
        primaire = self.preparer()
        jalon = self.inserer_jalon(primaire, "pc-eteint")
        avant = self.etat_cluster()
        avant_ecritures = self.ecritures()
        perdues: list[int] = []
        anomalies = []
        sante_min = 3
        pc_arrete = False
        pc_redemarre = False
        try:
            self.cible.arreter_pc()
            pc_arrete = True
            fin_attente = time.time() + self.options.duree_pc
            while time.time() < fin_attente:
                roles = self.roles()
                primaires = [nom for nom, role in roles.items() if role == "f"]
                if primaires != [primaire]:
                    anomalies.append("rôle primaire changé : %s" % primaires)
                if any(role is None for role in roles.values()):
                    anomalies.append("un nœud est devenu injoignable")
                sante = self.cible.etcd_sante()
                sante_min = min(sante_min, sante.sains)
                if not sante.lisible:
                    anomalies.append("etcd illisible pendant l'extinction du poste")
                elif sante.sains < 3:
                    anomalies.append("etcd %s" % sante.resume())
                time.sleep(1)
            apres_ecritures = self.ecritures()["nombre"]
            time.sleep(2)
            if self.ecritures()["nombre"] != apres_ecritures:
                anomalies.append("le poste éteint a continué d'écrire")
            self.cible.demarrer_pc()
            pc_redemarre = True
            if self.cible.ecrivain_disponible:
                self.attendre_reprise_ecriture(apres_ecritures)
                # B/P2 de la revue : le journal est en ajout ; tous les
                # identifiants confirmés avant l'extinction doivent survivre
                # au rallumage (une troncature les effacerait).
                apres_ids = set(self.ecritures()["ids"])
                perdues = sorted(set(avant_ecritures["ids"]) - apres_ids)
                if perdues:
                    anomalies.append(
                        "journal du poste tronqué : %d identifiant(s) confirmé(s) "
                        "perdu(s) au rallumage" % len(perdues)
                    )
            fin = self.attendre_sain(timeout=self.options.timeout_sain)
            jalon_final = self.jalon_present(fin, jalon)
        finally:
            if pc_arrete and not pc_redemarre:
                try:
                    self.cible.demarrer_pc()
                except Erreur as exc:
                    log("le poste n'a pas pu être rallumé : %s" % exc)
        if not jalon_final:
            anomalies.append("le jalon écrit avant l'extinction est absent")
        if fin != primaire:
            anomalies.append("le primaire a changé (%s → %s)" % (primaire, fin))
        verdict = "blocked" if anomalies else "ok"
        return self._resultat(
            "pc-eteint",
            time.time() - self.options.duree_pc - 1,
            {
                "duree_pc_eteint_s": self.options.duree_pc,
                "primaire_avant": avant["primaire"],
                "primaire_apres": fin,
                "etcd_sains_minimum": sante_min,
                "ecritures_avant": avant_ecritures["nombre"],
                "ecritures_pendant": apres_ecritures,
                "ecritures_confirmees_avant": len(avant_ecritures["ids"]),
                "ecritures_perdues_au_rallumage": len(perdues),
                "reprise_ecriture": True,
            },
            {
                "jalon_id": jalon,
                "jalon_sur_grappe_finale": jalon_final,
                "journal_integral_apres_rallumage": not perdues,
            },
            anomalies or ["Aucun effet sur le primaire, le quorum etcd ni la réplication."],
            verdict,
        )

    def attendre_reprise_ecriture(self, avant: int, timeout: float = 40) -> int:
        if not self.ecritures_fichier:
            return 0
        debut = time.time()
        while time.time() - debut < timeout:
            nombre = self.ecritures()["nombre"]
            if nombre > avant:
                return nombre
            time.sleep(0.5)
        raise Echec("le poste client n'a pas repris ses écritures après %.0f s" % timeout)

    def _scenario_deux_noeuds(self) -> dict:
        primaire = self.preparer()
        candidats = [n["nom"] for n in self.cible.noeuds if not n.get("nofailover")]
        survivants = [n["nom"] for n in self.cible.noeuds if n.get("nofailover")]
        if len(candidats) < 2 or not survivants:
            raise Echec("topologie inattendue : deux candidats et un nœud nofailover requis")
        victime2 = [nom for nom in candidats if nom != primaire][0]
        survivant = survivants[0]
        jalon = self.inserer_jalon(primaire, "deux-noeuds")
        avant = self.ecritures()
        surveillance = Surveillance(self.cible)
        tues = []
        redemarres = set()
        surveillance.start()
        try:
            t0 = time.time()
            self.cible.tuer(primaire)
            tues.append(primaire)
            self.cible.tuer(victime2)
            tues.append(victime2)
            time.sleep(self.options.delai_quorum)
            role_survivant = self.cible.role(survivant)
            sante_pendant = self.cible.etcd_sante()
            ecritures_pendant = self.ecritures()["nombre"]
            time.sleep(2)
            ecritures_stables = self.ecritures()["nombre"] == ecritures_pendant
            primaires_pendant = surveillance.noeuds_primaires()
            jalon_survivant = self.jalon_present(survivant, jalon)
            self.cible.demarrer_noeud(victime2)
            redemarres.add(victime2)
            self.cible.demarrer_noeud(primaire)
            redemarres.add(primaire)
            fin = self.attendre_sain(timeout=self.options.timeout_sain * 2)
            jalon_final = self.jalon_present(fin, jalon)
            split, periodes = surveillance.primaires_simultanes()
        finally:
            for nom in tues:
                if nom in redemarres:
                    continue
                try:
                    self.cible.demarrer_noeud(nom)
                except Erreur as exc:
                    log("retour de %s non tenté : %s" % (nom, exc))
            surveillance.arreter()
        anomalies = []
        if role_survivant == "f" or primaires_pendant:
            anomalies.append(
                "le survivant s'est déclaré primaire sans quorum : %s" % primaires_pendant
            )
        if not ecritures_stables:
            anomalies.append("des écritures ont continué sans quorum")
        if split:
            anomalies.append("deux primaires simultanés pendant %.1f s" % split)
        if not jalon_final:
            anomalies.append("le jalon écrit avant la perte est absent après retour")
        verdict = "blocked" if anomalies else "limite"
        notes = [
            "Limite attendue de l'étude : avec 1 votant etcd sur 3, la grappe ne peut "
            "pas promouvoir ; elle cesse d'écrire et repart au retour des deux nœuds.",
        ]
        if jalon_survivant:
            notes.append(
                "Le standby asynchrone survivant portait déjà le jalon (aucune avance "
                "de WAL perdue sur cette exécution)."
            )
        else:
            notes.append(
                "Le standby asynchrone survivant ne portait pas le dernier jalon : "
                "c'est la limite documentée du nœud nosync."
            )
        return self._resultat(
            "deux-noeuds",
            time.time() - self.options.delai_quorum - 1,
            {
                "survivant": survivant,
                "victime2": victime2,
                "role_survivant": role_survivant,
                "etcd_pendant": sante_pendant.resume(),
                "etcd_sains_pendant": sante_pendant.sains,
                "ecritures_pendant": ecritures_pendant,
                "ecritures_stables": ecritures_stables,
                "split_brain_s": round(split, 1),
                "rpo_apres_retour": jalon_final,
            },
            {
                "primaire_initial": primaire,
                "primaire_final": fin,
                "jalon_id": jalon,
                "jalon_survivant_asynchrone": jalon_survivant,
                "jalon_apres_retour": jalon_final,
                "periodes_deux_primaires": len(periodes),
            },
            notes,
            verdict,
        )


def _extrait(texte: str, motifs) -> list[str]:
    lignes = []
    for ligne in texte.splitlines():
        if any(motif in ligne.lower() for motif in motifs):
            lignes.append(ligne.strip()[:300])
    return lignes[-8:]


# ---------------------------------------------------------------------------
# Rapport Markdown.


def rendre_markdown(rapport: dict) -> str:
    environ = rapport.get("environnement", {})
    conclusion = rapport.get("conclusion", {})
    lignes = [
        "# Rapport d'exercice — grappe PostgreSQL du mesh (lot #6902)",
        "",
        "Scénarios EP8.6 rejoués sur %s le %s. Format `%s`."
        % (
            "le banc local jetable" if rapport.get("cible") == "banc" else "la grappe réelle",
            rapport.get("genere_le", "?"),
            rapport.get("schema", SCHEMA),
        ),
        "",
        "## Environnement",
        "",
        "| Élément | Valeur |",
        "|---|---|",
        "| Cible | `%s` |" % rapport.get("cible", "?"),
        "| Hôte | `%s` |" % environ.get("hote", "?"),
        "| Noyau | `%s` |" % environ.get("noyau", "?"),
        "| Docker | `%s` |" % str(environ.get("docker", "—")).replace("|", "\\|"),
        "| Accès | %s |" % str(environ.get("accessibilite", "?")).replace("|", "\\|"),
        "",
    ]
    if environ.get("versions"):
        lignes += ["```", str(environ["versions"]).strip(), "```", ""]
    if environ.get("noeuds"):
        lignes += ["Nœuds : " + ", ".join(
            "`%s` (%s%s)" % (
                n.get("nom"),
                n.get("adresse"),
                ", nofailover" if n.get("nofailover") else "",
            )
            for n in environ["noeuds"]
        ), ""]

    constats = rapport.get("constats") or []
    if constats:
        lignes += ["## Constats d'exécution", ""]
        for constat in constats:
            lignes.append(
                "- **%s** : `%s` → `%s`. Cause : %s ; correction : %s."
                % (
                    constat.get("fichier", "?"),
                    constat.get("avant", "?"),
                    constat.get("apres", "?"),
                    constat.get("cause", "?"),
                    constat.get("correction", "?"),
                )
            )
        lignes.append("")

    lignes += [
        "## Synthèse",
        "",
        "| Scénario | Verdict | Nouveau primaire | Cible < 60 s | RPO | Deux primaires | Retour |",
        "|---|---|---|---|---|---|---|",
    ]
    for scenario in rapport.get("scenarios", []):
        mesures = scenario.get("mesures", {})
        delai = mesures.get("delai_nouveau_primaire_s")
        lignes.append(
            "| %s | **%s** | %s | %s | %s | %s | %s |"
            % (
                scenario.get("nom", "?"),
                scenario.get("verdict", "?"),
                "—" if delai is None else "%.1f s" % delai,
                "oui" if mesures.get("cible_60s") else "non",
                "oui" if mesures.get("rpo_jalon") else "non",
                "%.1f s" % mesures["split_brain_s"] if "split_brain_s" in mesures else "—",
                (
                    "%.1f s" % mesures["retour_ancien_primaire_s"]
                    if mesures.get("retour_ancien_primaire_s") is not None
                    else "—"
                ),
            )
        )
    lignes += [
        "",
        "**Conclusion : %s — %s.**"
        % (conclusion.get("verdict", "?"), conclusion.get("resume", "")),
        "",
    ]

    lignes += ["## Détail par scénario", ""]
    for scenario in rapport.get("scenarios", []):
        lignes += [
            "### %s — %s" % (scenario.get("nom", "?"), scenario.get("verdict", "?")),
            "",
            "%s (%.1f s)." % (scenario.get("titre", ""), scenario.get("duree_s", 0)),
            "",
        ]
        if scenario.get("erreur"):
            lignes += ["Erreur : `%s`" % scenario["erreur"], ""]
        mesures = scenario.get("mesures") or {}
        if mesures:
            lignes += ["| Mesure | Valeur |", "|---|---|"]
            for cle, valeur in mesures.items():
                if isinstance(valeur, bool):
                    valeur = "oui" if valeur else "non"
                lignes.append("| `%s` | %s |" % (cle, valeur))
            lignes.append("")
        preuves = scenario.get("preuves") or {}
        if preuves:
            lignes += ["Preuves :", ""]
            for cle, valeur in preuves.items():
                if isinstance(valeur, list):
                    if valeur:
                        lignes.append("- `%s` :" % cle)
                        lignes += ["  - `%s`" % str(v).replace("`", "'") for v in valeur]
                else:
                    lignes.append("- `%s` : `%s`" % (cle, valeur))
            lignes.append("")
        for note in scenario.get("notes", []):
            lignes.append("- %s" % note)
        lignes.append("")

    lignes += [
        "## Limites",
        "",
        "- Le banc est local et jetable : il prouve le comportement de Patroni, etcd et "
        "PostgreSQL 17 avec les gabarits de #6870, pas la latence réelle des VM ni le "
        "réseau privé d'EP8.2.",
        "- La perte de deux nœuds n'est pas survivable par conception (1 votant etcd sur 3) ; "
        "le scénario vérifie l'absence de split brain et le retour, pas la disponibilité.",
        "- Le poste client du banc n'est pas le poste de l'owner : le scénario « PC éteint » "
        "vérifie qu'un client qui disparaît n'affecte pas la grappe.",
        "",
        "## Reproduction",
        "",
        "```bash",
        "ameesh heavy -- python3 deploy/serveur/patroni/banc/exercer.py --cible banc",
        "```",
        "",
        "Le rapport JSON de même contenu est écrit à côté de ce fichier (`rapport.json`).",
        "",
    ]
    return "\n".join(lignes)


def ecrire_rapport(rapport: dict, dossier: Path) -> tuple[Path, Path]:
    dossier.mkdir(parents=True, exist_ok=True)
    json_path = dossier / "rapport.json"
    md_path = dossier / "rapport.md"
    json_path.write_text(
        json.dumps(rapport, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_path.write_text(rendre_markdown(rapport), encoding="utf-8")
    return json_path, md_path


# ---------------------------------------------------------------------------
# Auto-test sans Docker : vérifie le rendu et le format du rapport.


def autotest(dossier: Path) -> dict:
    scenarios = []
    for nom, verdict, delai, split in (
        ("perte-primaire", "ok", 34.2, 0.0),
        ("coupure-reseau", "ok", 31.7, 0.0),
        ("pc-eteint", "ok", None, 0.0),
        ("deux-noeuds", "limite", None, 0.0),
    ):
        scenarios.append(
            {
                "nom": nom,
                "titre": TITRES[nom],
                "verdict": verdict,
                "duree_s": 42.0,
                "mesures": {
                    "delai_nouveau_primaire_s": delai,
                    "cible_60s": delai is not None and delai <= 60,
                    "rpo_jalon": True,
                    "split_brain_s": split,
                    "retour_ancien_primaire_s": 8.4 if nom == "perte-primaire" else None,
                },
                "preuves": {"jalon_id": 1},
                "notes": ["auto-test"],
            }
        )
    rapport = {
        "schema": SCHEMA,
        "lot": LOT,
        "cible": "banc",
        "genere_le": maintenant(),
        "environnement": {
            "hote": socket.gethostname(),
            "noyau": platform.release(),
            "python": platform.python_version(),
            "noeuds": [],
        },
        "constats": [],
        "scenarios": scenarios,
        "conclusion": {
            "verdict": "limite",
            "scenarios_executes": 4,
            "ok": 3,
            "limites": 1,
            "bloques": 0,
            "resume": "4 scénario(s) exécuté(s) : 3 ok, 1 limite(s), 0 bloqué(s)",
        },
    }
    ecrire_rapport(rapport, dossier)
    return rapport


# ---------------------------------------------------------------------------
# Ligne de commande.


def construire_parseur() -> argparse.ArgumentParser:
    parseur = argparse.ArgumentParser(
        description="Rejoue les scénarios EP8.6 sur le banc local (ou la grappe, sur option).",
        epilog="Sans --geste-reel, la cible grappe est refusée : le banc est la cible par défaut.",
    )
    parseur.add_argument("--cible", choices=("banc", "grappe"), default="banc")
    parseur.add_argument(
        "--geste-reel",
        action="store_true",
        help="autorise les gestes sur la grappe réelle (obligatoire avec --cible grappe)",
    )
    parseur.add_argument("--grappe-config", type=Path, help="fichier de la grappe réelle")
    parseur.add_argument(
        "--lecture-seule",
        action="store_true",
        help="cible grappe : vérifie l'accès et l'état, sans aucun geste",
    )
    parseur.add_argument("--etat", type=Path, help="dossier d'état du banc (défaut : mktemp)")
    parseur.add_argument("--rapport", type=Path, help="dossier du rapport (défaut : mktemp)")
    parseur.add_argument(
        "--scenarios",
        default=",".join(SCENARIOS),
        help="liste séparée par des virgules (défaut : les quatre)",
    )
    parseur.add_argument("--sans-reinitialiser", action="store_true")
    parseur.add_argument("--garder", action="store_true", help="garde le banc après l'exercice")
    parseur.add_argument("--duree-pc", type=int, default=20)
    parseur.add_argument("--autotest", action="store_true", help="rapport synthétique, sans Docker")
    parseur.add_argument("--json", action="store_true", help="résumé JSON sur la sortie standard")
    return parseur


def main(argv=None) -> int:
    arguments = construire_parseur().parse_args(argv)
    scenarios = tuple(s.strip() for s in arguments.scenarios.split(",") if s.strip())
    for nom in scenarios:
        if nom not in TITRES:
            print("scénario inconnu : %s" % nom, file=sys.stderr)
            return 2

    if arguments.autotest:
        dossier = arguments.rapport or Path(tempfile.mkdtemp(prefix="ameesh-banc-6902-rapport-"))
        rapport = autotest(Path(dossier))
        json_path, md_path = dossier / "rapport.json", dossier / "rapport.md"
        if arguments.json:
            print(json.dumps(rapport["conclusion"], ensure_ascii=False))
        print("rapport : %s\n          %s" % (json_path, md_path))
        return 0

    if arguments.cible == "banc" and arguments.geste_reel:
        print(
            "--geste-reel ne s'applique qu'à la cible grappe ; le banc est toujours local.",
            file=sys.stderr,
        )
        return 2
    if arguments.cible == "grappe":
        if not arguments.grappe_config:
            print("refus : --grappe-config requis avec --cible grappe.", file=sys.stderr)
            return 2
        try:
            cible = GrappeCible(arguments.grappe_config)
        except Erreur as exc:
            print("grappe : %s" % exc, file=sys.stderr)
            return 2
        if arguments.lecture_seule:
            # Lecture seule : aucun geste, donc pas d'option de geste réel.
            print("accès : %s" % cible.accessibilite())
            try:
                primaire = next(
                    (n for n in cible.noeuds_noms if cible.role(n) == "f"), None
                )
            except Erreur as exc:
                print("lecture : %s" % exc, file=sys.stderr)
                return 1
            print("primaire : %s" % (primaire or "aucun"))
            print("etcd : %s" % cible.etcd_sante().resume())
            return 0 if primaire else 1
        if not arguments.geste_reel:
            print(
                "refus : la cible grappe exige --geste-reel explicite ; par défaut, "
                "le banc local seulement.",
                file=sys.stderr,
            )
            return 2
        etat = None
    else:
        if arguments.lecture_seule:
            print("--lecture-seule ne s'applique qu'à la cible grappe.", file=sys.stderr)
            return 2
        etat = arguments.etat or Path(tempfile.mkdtemp(prefix="ameesh-banc-6902-"))
        try:
            cible = BancCible(etat)
        except Erreur as exc:
            print("banc : %s" % exc, file=sys.stderr)
            return 2

    options = Options(
        reinitialiser=not arguments.sans_reinitialiser,
        duree_pc=max(5, arguments.duree_pc),
        scenarios=scenarios,
    )
    dossier_rapport = arguments.rapport or Path(
        tempfile.mkdtemp(prefix="ameesh-banc-6902-rapport-")
    )
    code = 0
    try:
        cible.demarrer()
        pilote = Pilote(cible, options, etat=etat)
        rapport = pilote.executer()
        json_path, md_path = ecrire_rapport(rapport, dossier_rapport)
        print(rendre_markdown(rapport)[:2000])
        print("\nrapport complet : %s\n                  %s" % (json_path, md_path))
        if arguments.json:
            print(json.dumps(rapport["conclusion"], ensure_ascii=False))
        code = 0 if rapport["conclusion"]["verdict"] in ("ok", "limite") else 1
    except Exception as exc:  # noqa: BLE001 - un rapport d'échec vaut mieux qu'une trace
        print("exercice interrompu : %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        rapport = {
            "schema": SCHEMA,
            "lot": LOT,
            "cible": cible.nom,
            "genere_le": maintenant(),
            "environnement": {"hote": socket.gethostname()},
            "constats": [],
            "scenarios": [],
            "conclusion": {
                "verdict": "blocked",
                "scenarios_executes": 0,
                "ok": 0,
                "limites": 0,
                "bloques": 1,
                "resume": "%s: %s" % (type(exc).__name__, exc),
            },
        }
        json_path, md_path = ecrire_rapport(rapport, dossier_rapport)
        print("rapport d'échec : %s" % json_path, file=sys.stderr)
        code = 1
    finally:
        if not arguments.garder:
            try:
                cible.arreter()
            except Erreur as exc:
                log("nettoyage : %s" % exc)
        else:
            log("banc conservé : %s" % etat)
    return code


if __name__ == "__main__":
    sys.exit(main())
