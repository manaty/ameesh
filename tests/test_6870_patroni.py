# SPDX-License-Identifier: AGPL-3.0-only
"""#6870 (EP8.4b) — préparation etcd + Patroni, sans geste réel.

Les vrais scripts de `deploy/serveur/patroni/` sont exécutés hors ligne :
génération des configurations, matériel TLS jetable, enveloppe `patronictl`,
unités systemd et collecteur de santé. Aucun réseau, aucune base, aucun root,
aucune unité réelle. Patroni n'est pas une dépendance du projet : sa
validation est testée quand le module est présent, ignorée sinon (canon §3).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover - le venv de contrats le fournit
    yaml = None

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIR = ROOT / "deploy" / "serveur" / "patroni"
GENERATEUR = DIR / "generer-configuration.py"
VALIDATEUR = DIR / "valider-modeles.py"
TLS = DIR / "generer-tls.sh"
SANTE = DIR / "sante-base.py"
WRAPPER = DIR / "patronictl"
TOPOLOGIE = DIR / "topologie.json"

REST = "RestPassword0123456789abcdef"
REPL = "ReplPassword0123456789abcdef"
SUPER = "SuperPassword0123456789abcdef"
SECRETS = """\
PATRONI_RESTAPI_USERNAME=patroni
PATRONI_RESTAPI_PASSWORD=%s
PATRONI_REPLICATION_USERNAME=replicator
PATRONI_REPLICATION_PASSWORD=%s
PATRONI_SUPERUSER_PASSWORD=%s
""" % (REST, REPL, SUPER)


def _env(**extra):
    base = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(extra.pop("home", pathlib.Path(tempfile.gettempdir()))),
        "TMPDIR": os.environ.get("TMPDIR", tempfile.gettempdir()),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    base.update(extra)
    return base


def _run(args, env=None, cwd=None, timeout=120):
    return subprocess.run(args, capture_output=True, text=True,
                          env=_env(**(env or {})), cwd=str(cwd) if cwd else None,
                          timeout=timeout)


def _topologie(dossier):
    topologie = json.loads(TOPOLOGIE.read_text(encoding="utf-8"))
    chemin = pathlib.Path(dossier) / "topologie.json"
    chemin.write_text(json.dumps(topologie, ensure_ascii=False), encoding="utf-8")
    return topologie, chemin


def _secrets(dossier, contenu=SECRETS, mode=0o600):
    chemin = pathlib.Path(dossier) / "secrets.env"
    chemin.write_text(contenu, encoding="utf-8")
    chemin.chmod(mode)
    return chemin


def _charger(chemin, nom):
    spec = importlib.util.spec_from_file_location(nom, chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _yaml(chemin):
    if yaml is None:
        raise unittest.SkipTest("PyYAML absent")
    return yaml.safe_load(pathlib.Path(chemin).read_text(encoding="utf-8"))


class ConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="t6870-config-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.topologie, self.topologie_fichier = _topologie(self.tmp)
        self.secrets = _secrets(self.tmp)
        self.sortie = self.tmp / "config"

    def generer(self, *options, secrets=None, topologie=None, env=None):
        commande = [sys.executable, str(GENERATEUR),
                    "--topologie", str(topologie or self.topologie_fichier),
                    "--secrets", str(secrets or self.secrets),
                    "--sortie", str(self.sortie), *options]
        return _run(commande, env=env)

    def test_trois_noeuds_fichiers_et_permissions(self):
        resultat = self.generer("--tous", "--verifier")
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        for noeud in self.topologie["noeuds"]:
            dossier = self.sortie / noeud["nom"]
            self.assertTrue(dossier.is_dir())
            for nom, mode in (("patroni.yml", 0o640), ("etcd.yml", 0o640),
                              ("patroni.env", 0o600)):
                fichier = dossier / nom
                self.assertTrue(fichier.is_file(), fichier)
                self.assertEqual(stat.S_IMODE(fichier.stat().st_mode), mode, fichier)
        self.assertEqual(stat.S_IMODE(self.sortie.stat().st_mode), 0o750)

    def test_etcd_trois_membres_tls_et_adresse_privee(self):
        self.assertEqual(self.generer("--tous").returncode, 0)
        ports = self.topologie["ports"]
        for noeud in self.topologie["noeuds"]:
            etcd = _yaml(self.sortie / noeud["nom"] / "etcd.yml")
            self.assertEqual(etcd["name"], noeud["nom"])
            self.assertEqual(etcd["listen-client-urls"],
                             "https://%s:%d" % (noeud["adresse"], ports["etcd_client"]))
            self.assertEqual(etcd["advertise-client-urls"],
                             "https://%s:%d" % (noeud["adresse"], ports["etcd_client"]))
            self.assertEqual(etcd["listen-peer-urls"],
                             "https://%s:%d" % (noeud["adresse"], ports["etcd_peer"]))
            self.assertEqual(etcd["initial-advertise-peer-urls"],
                             "https://%s:%d" % (noeud["adresse"], ports["etcd_peer"]))
            membres = etcd["initial-cluster"].split(",")
            self.assertEqual(len(membres), 3)
            for autre in self.topologie["noeuds"]:
                self.assertIn("%s=https://%s:%d" % (
                    autre["nom"], autre["adresse"], ports["etcd_peer"]), membres)
            self.assertEqual(etcd["initial-cluster-state"], "new")
            for section in ("client-transport-security", "peer-transport-security"):
                self.assertTrue(etcd[section]["client-cert-auth"])
                self.assertEqual(etcd[section]["trusted-ca-file"], "/etc/ameesh/tls/ca.crt")
                self.assertFalse(etcd[section]["auto-tls"])
            texte = (self.sortie / noeud["nom"] / "etcd.yml").read_text(encoding="utf-8")
            for interdit in ("0.0.0.0", "127.0.0.1", "localhost", "[::]"):
                self.assertNotIn(interdit, texte)

    def test_patroni_reglages_de_l_etude(self):
        self.assertEqual(self.generer("--tous").returncode, 0)
        ports = self.topologie["ports"]
        for noeud in self.topologie["noeuds"]:
            patroni = _yaml(self.sortie / noeud["nom"] / "patroni.yml")
            self.assertEqual(patroni["scope"], "ameesh")
            self.assertEqual(patroni["namespace"], "/ameesh/")
            self.assertEqual(patroni["name"], noeud["nom"])
            self.assertEqual(patroni["restapi"]["listen"],
                             "%s:%d" % (noeud["adresse"], ports["patroni_rest"]))
            self.assertEqual(patroni["restapi"]["connect_address"],
                             "%s:%d" % (noeud["adresse"], ports["patroni_rest"]))
            self.assertIn("certfile", patroni["restapi"])
            self.assertIn("keyfile", patroni["restapi"])
            self.assertEqual(patroni["etcd3"]["protocol"], "https")
            hotes = patroni["etcd3"]["hosts"].split(",")
            self.assertEqual(len(hotes), 3)
            # Patroni 4.x attend hôte:port ; le schéma vient de protocol (#6925).
            self.assertEqual(hotes[0], "%s:%d" % (noeud["adresse"], ports["etcd_client"]))
            self.assertNotIn("://", patroni["etcd3"]["hosts"])
            self.assertNotIn("password", patroni["restapi"])
            self.assertNotIn("password", patroni["postgresql"]["authentication"]["replication"])
            self.assertEqual(patroni["postgresql"]["authentication"]["replication"]["username"],
                             "replicator")
            self.assertNotIn("rewind", patroni["postgresql"]["authentication"])
            self.assertNotIn("password", patroni["postgresql"]["authentication"]["superuser"])
            self.assertEqual(patroni["postgresql"]["connect_address"],
                             "%s:%d" % (noeud["adresse"], ports["postgres"]))
            # Patroni 4.x applique split_host_port une seule fois : le port ne
            # doit apparaître qu'à la fin, sinon PostgreSQL reçoit un hôte
            # « 127.0.0.1:5432 » invalide.
            self.assertEqual(patroni["postgresql"]["listen"],
                             "127.0.0.1,%s:%d" % (noeud["adresse"], ports["postgres"]))
            hotes_pg, port_pg = patroni["postgresql"]["listen"].rsplit(":", 1)
            self.assertNotIn(":", hotes_pg)
            self.assertEqual(int(port_pg), ports["postgres"])
            # Connexions locales de Patroni par socket Unix (peer) : sans
            # cela, le superutilisateur devrait avoir un mot de passe TCP local
            # et le pg_hba localhost suffirait à peine.
            self.assertTrue(patroni["postgresql"]["use_unix_socket"])
            dcs = patroni["bootstrap"]["dcs"]
            self.assertEqual(dcs["ttl"], 30)
            self.assertEqual(dcs["loop_wait"], 10)
            self.assertEqual(dcs["retry_timeout"], 10)
            self.assertEqual(dcs["maximum_lag_on_failover"], 1048576)
            self.assertTrue(dcs["synchronous_mode"])
            self.assertEqual(dcs["synchronous_node_count"], 1)
            self.assertFalse(dcs["synchronous_mode_strict"])
            self.assertFalse(dcs["failsafe_mode"])
            self.assertTrue(dcs["postgresql"]["use_pg_rewind"])
            self.assertTrue(dcs["postgresql"]["use_slots"])
            regles = patroni["postgresql"]["pg_hba"]
            self.assertTrue(any(regle.startswith("hostssl replication replicator 10.78.0.0/24")
                                for regle in regles))
            # Patroni lit la timeline par une connexion de réplication locale
            # en TCP localhost ; sans règle dédiée, get_replica_timeline échoue.
            self.assertIn("hostssl replication replicator 127.0.0.1/32 scram-sha-256", regles)
            self.assertIn("hostssl replication replicator ::1/128 scram-sha-256", regles)
            # pg_rewind se connecte au primaire distant en superutilisateur.
            self.assertIn("hostssl all all 127.0.0.1/32 scram-sha-256", regles)
            self.assertIn("local all postgres peer", regles)
            self.assertFalse(any("trust" in regle for regle in regles))
            self.assertEqual(patroni["watchdog"]["mode"], "off")
            if noeud["nofailover"]:
                self.assertTrue(patroni["tags"]["nofailover"])
                self.assertTrue(patroni["tags"]["nosync"])
                self.assertNotIn("failover_priority", patroni["tags"])
            else:
                self.assertEqual(patroni["tags"]["failover_priority"], noeud["priorite"])
                self.assertNotIn("nofailover", patroni["tags"])

    def test_primaire_prefere_est_la_priorite_maximale(self):
        self.assertEqual(self.generer("--tous").returncode, 0)
        priorites = {}
        for noeud in self.topologie["noeuds"]:
            patroni = _yaml(self.sortie / noeud["nom"] / "patroni.yml")
            tags = patroni["tags"]
            priorites[noeud["nom"]] = 0 if tags.get("nofailover") else tags["failover_priority"]
        self.assertEqual(max(priorites, key=priorites.get), "ameesh-agents-1")
        self.assertEqual(priorites["ameesh-agents-1"], 100)
        self.assertEqual(priorites["ameesh-agents-2"], 90)
        self.assertEqual(priorites["manaty-mesh-1"], 0)

    def test_patroni_lit_le_listen_et_le_port(self):
        """Paramètres effectifs : split_host_port (Patroni 4.1.5) doit rendre
        deux hôtes et un port, jamais « 127.0.0.1:5432 » comme premier hôte."""
        try:
            from patroni.utils import split_host_port
        except ImportError:
            self.skipTest("Patroni absent : validation effective impossible")
        self.assertEqual(self.generer("--tous").returncode, 0)
        ports = self.topologie["ports"]
        for noeud in self.topologie["noeuds"]:
            patroni = _yaml(self.sortie / noeud["nom"] / "patroni.yml")
            adresses, port = split_host_port(patroni["postgresql"]["listen"],
                                             ports["postgres"])
            self.assertEqual(adresses, "127.0.0.1,%s" % noeud["adresse"])
            self.assertEqual(port, ports["postgres"])

    def test_aucun_secret_dans_les_yaml_et_secrets_dans_env(self):
        self.assertEqual(self.generer("--tous").returncode, 0)
        for noeud in self.topologie["noeuds"]:
            patroni = (self.sortie / noeud["nom"] / "patroni.yml").read_text(encoding="utf-8")
            env = (self.sortie / noeud["nom"] / "patroni.env").read_text(encoding="utf-8")
            self.assertNotIn(REST, patroni)
            self.assertNotIn(REPL, patroni)
            self.assertNotIn(SUPER, patroni)
            self.assertNotRegex(patroni, r"(?m)^\s*password\s*:")
            self.assertIn("PATRONI_RESTAPI_PASSWORD=%s" % REST, env)
            self.assertIn("PATRONI_REPLICATION_PASSWORD=%s" % REPL, env)
            self.assertIn("PATRONI_SUPERUSER_PASSWORD=%s" % SUPER, env)
            self.assertIn("AMEESH_DB_REST_URL=https://%s:%d" % (
                noeud["adresse"], self.topologie["ports"]["patroni_rest"]), env)
            self.assertIn("AMEESH_DB_ETCD_ENDPOINTS=", env)

    def test_aucune_cle_privee_dans_les_sources(self):
        for fichier in DIR.rglob("*"):
            if not fichier.is_file() or fichier.suffix in (".pyc",):
                continue
            texte = fichier.read_text(encoding="utf-8", errors="replace")
            self.assertNotRegex(texte, r"-----BEGIN [A-Z ]*PRIVATE KEY-----", fichier)

    def test_secrets_trop_permissifs_refuses(self):
        self.secrets.chmod(0o644)
        resultat = self.generer("--tous")
        self.assertNotEqual(resultat.returncode, 0)
        self.assertIn("permissions", resultat.stderr)
        self.assertFalse((self.sortie / "ameesh-agents-1").exists())

    def test_secret_manquant_ou_court_refuse(self):
        self.secrets.write_text(SECRETS.replace("PATRONI_RESTAPI_PASSWORD", "AUTRE"), encoding="utf-8")
        self.secrets.chmod(0o600)
        self.assertNotEqual(self.generer("--tous").returncode, 0)
        self.secrets.write_text(SECRETS.replace(REST, "court"), encoding="utf-8")
        self.secrets.chmod(0o600)
        resultat = self.generer("--tous")
        self.assertNotEqual(resultat.returncode, 0)
        self.assertIn("trop court", resultat.stderr)

    def test_quatrieme_votant_refuse(self):
        topologie = json.loads(self.topologie_fichier.read_text(encoding="utf-8"))
        topologie["noeuds"].append({
            "nom": "temoins-4", "adresse": "10.78.0.14", "role": "temoin",
            "priorite": 10, "nofailover": False, "nosync": False})
        self.topologie_fichier.write_text(json.dumps(topologie), encoding="utf-8")
        resultat = self.generer("--tous")
        self.assertNotEqual(resultat.returncode, 0)
        self.assertIn("trois votants", resultat.stderr)

    def test_primaire_prefere_incoherent_refuse(self):
        topologie = json.loads(self.topologie_fichier.read_text(encoding="utf-8"))
        topologie["primaire_prefere"] = "ameesh-agents-2"
        self.topologie_fichier.write_text(json.dumps(topologie), encoding="utf-8")
        resultat = self.generer("--tous")
        self.assertNotEqual(resultat.returncode, 0)
        self.assertIn("primaire_prefere", resultat.stderr)

    def test_nosync_sans_nofailover_refuse(self):
        topologie = json.loads(self.topologie_fichier.read_text(encoding="utf-8"))
        topologie["noeuds"][0]["nosync"] = True
        self.topologie_fichier.write_text(json.dumps(topologie), encoding="utf-8")
        resultat = self.generer("--tous")
        self.assertNotEqual(resultat.returncode, 0)
        self.assertIn("nosync", resultat.stderr)

    def test_liste_noeuds(self):
        resultat = _run([sys.executable, str(GENERATEUR), "--topologie",
                         str(self.topologie_fichier), "--liste-noeuds"])
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        lignes = [ligne.split("\t") for ligne in resultat.stdout.splitlines()]
        self.assertEqual(len(lignes), 3)
        self.assertEqual(lignes[0], ["ameesh-agents-1", "10.78.0.11"])
        self.assertEqual(lignes[2], ["manaty-mesh-1", "10.78.0.10"])

    def test_noeud_unique(self):
        resultat = self.generer("--noeud", "ameesh-agents-1", "--verifier")
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        self.assertTrue((self.sortie / "patroni.yml").is_file())
        self.assertTrue((self.sortie / "etcd.yml").is_file())
        self.assertFalse((self.sortie / "ameesh-agents-2").exists())

    def test_verifier_appelle_le_validateur_patroni(self):
        journal = self.tmp / "journal"
        faux = self.tmp / "faux-python"
        faux.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$JOURNAL\"\nexit 0\n")
        faux.chmod(0o755)
        resultat = self.generer("--tous", "--verifier", env={
            "PATRONI_PYTHON": str(faux), "JOURNAL": str(journal)})
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        appels = journal.read_text(encoding="utf-8")
        self.assertIn("-c import patroni", appels)
        self.assertIn("valider-modeles.py", appels)

    def test_verifier_structure_detecte_un_ecart(self):
        module = _charger(GENERATEUR, "generer_configuration_6870")
        topologie = module.charger_topologie(self.topologie_fichier)
        secrets = module.charger_secrets(self.secrets)
        resultat = self.generer("--tous")
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        fichiers = {}
        for noeud in topologie["noeuds"]:
            dossier = self.sortie / noeud["nom"]
            fichiers[noeud["nom"]] = {
                "patroni": dossier / "patroni.yml",
                "etcd": dossier / "etcd.yml",
                "env": dossier / "patroni.env",
            }
        patroni = fichiers["ameesh-agents-1"]["patroni"]
        patroni.write_text(patroni.read_text(encoding="utf-8").replace(
            "synchronous_node_count: 1", "synchronous_node_count: 2"))
        with self.assertRaisesRegex(module.Erreur, "synchronous_node_count"):
            module.verifier_structure(topologie, secrets, fichiers)
        self.assertEqual(self.generer("--tous").returncode, 0)
        etcd = fichiers["manaty-mesh-1"]["etcd"]
        etcd.write_text(etcd.read_text(encoding="utf-8").replace(
            "https://10.78.0.10:2379", "http://0.0.0.0:2379"))
        with self.assertRaisesRegex(module.Erreur, "interdite|incomplète|privée"):
            module.verifier_structure(topologie, secrets, fichiers)

    @unittest.skipUnless(importlib.util.find_spec("patroni"), "module Patroni absent")
    def test_valider_modeles_avec_patroni_si_present(self):
        self.assertEqual(self.generer("--tous").returncode, 0)
        fichiers = [str(self.sortie / noeud["nom"] / "patroni.yml")
                    for noeud in self.topologie["noeuds"]]
        resultat = _run([sys.executable, str(VALIDATEUR)] + fichiers)
        self.assertEqual(resultat.returncode, 0, resultat.stderr)


class TlsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="t6870-tls-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._topologie, self.topologie_fichier = _topologie(self.tmp)
        self.sortie = self.tmp / "tls"

    def generer(self, *options):
        return _run(["bash", str(TLS), "--topologie", str(self.topologie_fichier),
                     "--sortie", str(self.sortie), *options])

    def test_permissions_et_empreintes(self):
        resultat = self.generer()
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        self.assertEqual(stat.S_IMODE(self.sortie.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.sortie / "ca.key").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.sortie / "secrets.env").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.sortie / "ca.crt").stat().st_mode), 0o644)
        for noeud in self._topologie["noeuds"]:
            for base in ("etcd-server", "etcd-client", "patroni-rest"):
                self.assertEqual(stat.S_IMODE(
                    (self.sortie / noeud["nom"] / (base + ".key")).stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(
                    (self.sortie / noeud["nom"] / (base + ".crt")).stat().st_mode), 0o644)
        verif = _run(["sh", "-c", "cd \"$1\" && sha256sum -c empreintes.sha256",
                      "sh", str(self.sortie)])
        self.assertEqual(verif.returncode, 0, verif.stderr)

    def test_certificats_signes_et_usages(self):
        self.assertEqual(self.generer().returncode, 0)
        for noeud in self._topologie["noeuds"]:
            for base in ("etcd-server", "etcd-client", "patroni-rest"):
                cert = self.sortie / noeud["nom"] / (base + ".crt")
                verif = _run(["openssl", "verify", "-CAfile",
                              str(self.sortie / "ca.crt"), str(cert)])
                self.assertEqual(verif.returncode, 0, verif.stderr)
                texte = _run(["openssl", "x509", "-in", str(cert), "-noout", "-text"]).stdout
                self.assertIn("IP Address:%s" % noeud["adresse"], texte)
                if base == "etcd-server":
                    self.assertIn("TLS Web Server Authentication", texte)
                    self.assertIn("TLS Web Client Authentication", texte)
                elif base == "etcd-client":
                    self.assertIn("TLS Web Client Authentication", texte)
                    self.assertNotIn("TLS Web Server Authentication", texte)
                else:
                    self.assertIn("TLS Web Server Authentication", texte)
                    self.assertNotIn("TLS Web Client Authentication", texte)

    def test_secrets_aleatoires_et_sans_caractere_shell(self):
        self.assertEqual(self.generer().returncode, 0)
        premier = (self.sortie / "secrets.env").read_text(encoding="utf-8")
        autre = self.tmp / "tls2"
        second = _run(["bash", str(TLS), "--topologie", str(self.topologie_fichier),
                       "--sortie", str(autre)])
        self.assertEqual(second.returncode, 0, second.stderr)
        deuxieme = (autre / "secrets.env").read_text(encoding="utf-8")
        self.assertNotEqual(premier, deuxieme)
        valeurs = {}
        for ligne in premier.splitlines():
            if ligne.startswith("PATRONI_"):
                cle, valeur = ligne.split("=", 1)
                valeurs[cle] = valeur
        self.assertEqual(valeurs["PATRONI_RESTAPI_USERNAME"], "patroni")
        self.assertEqual(valeurs["PATRONI_REPLICATION_USERNAME"], "replicator")
        for cle in ("PATRONI_RESTAPI_PASSWORD", "PATRONI_REPLICATION_PASSWORD",
                    "PATRONI_SUPERUSER_PASSWORD"):
            self.assertRegex(valeurs[cle], r"^[A-Za-z0-9]{32}$")
        self.assertEqual(len({valeurs["PATRONI_RESTAPI_PASSWORD"],
                              valeurs["PATRONI_REPLICATION_PASSWORD"],
                              valeurs["PATRONI_SUPERUSER_PASSWORD"]}), 3)

    def test_refus_sans_force_puis_force(self):
        self.assertEqual(self.generer().returncode, 0)
        avant = hashlib.sha256((self.sortie / "ca.crt").read_bytes()).hexdigest()
        refuse = self.generer()
        self.assertNotEqual(refuse.returncode, 0)
        self.assertIn("non vide", refuse.stderr)
        self.assertEqual(hashlib.sha256((self.sortie / "ca.crt").read_bytes()).hexdigest(), avant)
        force = self.generer("--force")
        self.assertEqual(force.returncode, 0, force.stderr)
        self.assertNotEqual(hashlib.sha256((self.sortie / "ca.crt").read_bytes()).hexdigest(), avant)

    def test_sortie_racine_refusee(self):
        resultat = _run(["bash", str(TLS), "--topologie", str(self.topologie_fichier),
                         "--sortie", "/"])
        self.assertNotEqual(resultat.returncode, 0)
        self.assertIn("refusée", resultat.stderr)

    def test_sortie_non_vide_refusee(self):
        self.sortie.mkdir()
        temoin = self.sortie / "temoin"
        temoin.write_text("x")
        resultat = self.generer()
        self.assertNotEqual(resultat.returncode, 0)
        self.assertEqual(temoin.read_text(), "x")


class WrapperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="t6870-ctl-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.conf = self.tmp / "patroni.yml"
        self.conf.write_text("scope: ameesh\n")
        self.env = self.tmp / "patroni.env"
        self.env.write_text("PATRONI_RESTAPI_USERNAME=patroni\n"
                            "PATRONI_RESTAPI_PASSWORD=mot-de-passe-de-test\n")
        self.journal = self.tmp / "journal"
        self.faux = self.tmp / "patronictl"
        self.faux.write_text("#!/bin/sh\nprintf 'arg=%s\\n' \"$*\" >> \"$JOURNAL\"\n"
                             "printf 'env=%s\\n' \"${PATRONI_RESTAPI_PASSWORD:-}\" >> \"$JOURNAL\"\n")
        self.faux.chmod(0o755)

    def lancer(self, *arguments, **env):
        return _run(["bash", str(WRAPPER), *arguments], env={
            "PATRONI_CONFIG_FILE": str(self.conf),
            "PATRONI_ENV_FILE": str(self.env),
            "PATRONI_CTL_BIN": str(self.faux),
            "JOURNAL": str(self.journal),
            **env,
        })

    def test_enveloppe_transmet_configuration_et_secrets(self):
        resultat = self.lancer("list")
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        journal = self.journal.read_text(encoding="utf-8")
        self.assertIn("arg=--config-file %s list" % self.conf, journal)
        self.assertIn("env=mot-de-passe-de-test", journal)

    def test_enveloppe_refuse_configuration_absente(self):
        self.conf.unlink()
        resultat = self.lancer("list")
        self.assertNotEqual(resultat.returncode, 0)
        self.assertIn("configuration illisible", resultat.stderr)

    def test_enveloppe_refuse_binaire_absent(self):
        resultat = self.lancer("list", PATRONI_CTL_BIN=str(self.tmp / "absent"))
        self.assertNotEqual(resultat.returncode, 0)
        self.assertIn("introuvable", resultat.stderr)


class UnitsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="t6870-units-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.unites = {
            nom: (DIR / nom).read_text(encoding="utf-8")
            for nom in ("etcd.service", "patroni.service",
                        "ameesh-db-sante.service", "ameesh-db-sante.timer")
        }

    def test_patroni_ne_requires_pas_etcd(self):
        unit = self.unites["patroni.service"]
        self.assertIn("User=postgres", unit)
        self.assertIn("EnvironmentFile=/etc/ameesh/patroni.env", unit)
        self.assertIn("ExecStart=/opt/patroni/venv/bin/patroni /etc/patroni/patroni.yml", unit)
        self.assertIn("After=network-online.target etcd.service", unit)
        self.assertNotRegex(unit, r"(?m)^Requires=etcd")
        self.assertIn("ProtectSystem=full", unit)
        self.assertIn("ReadWritePaths=/etc/postgresql", unit)

    def test_etcd_et_sante(self):
        etcd = self.unites["etcd.service"]
        self.assertIn("Type=notify", etcd)
        self.assertIn("User=etcd", etcd)
        self.assertIn("ExecStart=/usr/local/bin/etcd --config-file /etc/etcd/etcd.conf.yml", etcd)
        self.assertIn("StateDirectory=etcd", etcd)
        sante = self.unites["ameesh-db-sante.service"]
        self.assertIn("ExecStart=/usr/local/sbin/ameesh-db-sante --json", sante)
        self.assertIn("StateDirectory=ameesh-db", sante)
        self.assertIn("RuntimeDirectory=ameesh-db", sante)
        self.assertIn("RuntimeDirectoryPreserve=yes", sante)
        timer = self.unites["ameesh-db-sante.timer"]
        self.assertIn("OnUnitActiveSec=1min", timer)
        self.assertIn("Unit=ameesh-db-sante.service", timer)

    @unittest.skipUnless(
        shutil.which("systemd-run") and os.environ.get("XDG_RUNTIME_DIR")
        and os.environ.get("DBUS_SESSION_BUS_ADDRESS"),
        "gestionnaire systemd utilisateur absent")
    def test_repertoire_alertes_survit_au_cycle_de_l_unite(self):
        """Cycle réel (systemd 255) : preserve=yes garde alertes.json après la
        sonde oneshot ; le témoin sans preserve est bien supprimé."""
        sante = self.unites["ameesh-db-sante.service"]
        self.assertIn("RuntimeDirectoryPreserve=yes", sante)
        base = pathlib.Path(os.environ["XDG_RUNTIME_DIR"])
        nom = "ameesh-6870-cycle-%d-%d" % (os.getpid(), int(time.time()))
        env = {"XDG_RUNTIME_DIR": str(base),
               "DBUS_SESSION_BUS_ADDRESS": os.environ["DBUS_SESSION_BUS_ADDRESS"]}
        commande = ["systemd-run", "--user", "--wait", "--collect",
                    "--property=Type=oneshot"]
        temoin = nom + "-sans-preserve"
        shutil.rmtree(base / nom, ignore_errors=True)
        shutil.rmtree(base / temoin, ignore_errors=True)
        try:
            resultat = _run(commande + [
                "--unit", nom, "--property=RuntimeDirectory=" + nom,
                "--property=RuntimeDirectoryPreserve=yes",
                "/bin/sh", "-c",
                'printf ok > "$XDG_RUNTIME_DIR/%s/alertes.json"' % nom,
            ], env=env, timeout=30)
            self.assertEqual(resultat.returncode, 0, resultat.stderr)
            self.assertEqual(
                (base / nom / "alertes.json").read_text(encoding="utf-8"), "ok")
            resultat = _run(commande + [
                "--unit", temoin, "--property=RuntimeDirectory=" + temoin,
                "/bin/sh", "-c",
                'printf ok > "$XDG_RUNTIME_DIR/%s/alertes.json"' % temoin,
            ], env=env, timeout=30)
            self.assertEqual(resultat.returncode, 0, resultat.stderr)
            self.assertFalse((base / temoin).exists())
        finally:
            shutil.rmtree(base / nom, ignore_errors=True)
            shutil.rmtree(base / temoin, ignore_errors=True)

    def test_aucun_secret_dans_les_unites(self):
        for nom, texte in self.unites.items():
            self.assertNotRegex(texte, r"(?i)password\s*=", nom)
            self.assertNotRegex(texte, r"-----BEGIN", nom)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze absent")
    def test_syntaxe_systemd(self):
        copies = []
        for nom, texte in self.unites.items():
            texte = re.sub(r"(?m)^ExecStart=.*$", "ExecStart=/bin/true", texte)
            texte = re.sub(r"(?m)^EnvironmentFile=.*$", "EnvironmentFile=-/dev/null", texte)
            copie = self.tmp / nom
            copie.write_text(texte, encoding="utf-8")
            copies.append(str(copie))
        resultat = _run(["systemd-analyze", "verify", *copies])
        self.assertEqual(resultat.returncode, 0, resultat.stderr)


class SanteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="t6870-sante-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.module = _charger(SANTE, "sante_base_6870")
        self.etat = self.tmp / "etat.json"
        self.lecture = {
            "grappe": "ameesh",
            "patroni": {"leader": "ameesh-agents-1", "membres": [
                {"nom": "ameesh-agents-1", "role": "leader", "etat": "running", "retard": None},
                {"nom": "ameesh-agents-2", "role": "replica", "etat": "streaming", "retard": 0},
                {"nom": "manaty-mesh-1", "role": "replica", "etat": "streaming",
                 "retard": 1048576},
            ]},
            "etcd": [
                {"url": "https://10.78.0.11:2379", "sain": True, "detail": "ok"},
                {"url": "https://10.78.0.12:2379", "sain": True, "detail": "ok"},
                {"url": "https://10.78.0.10:2379", "sain": True, "detail": "ok"},
            ],
            "disque": {"chemin": "/var/lib/postgresql/17/main",
                       "total": 100 * self.module.GIO, "libre": 50 * self.module.GIO},
            "archivage": {"dernier_succes": 900, "dernier_echec": None},
        }

    def analyser(self, lecture=None, etat=None, maintenant=1000):
        return self.module.analyser(lecture if lecture is not None else self.lecture,
                                    etat if etat is not None else {}, maintenant)

    def types(self, alertes):
        return [alerte["type"] for alerte in alertes]

    def test_grappe_saine(self):
        alertes, etat = self.analyser()
        self.assertEqual(alertes, [])
        self.assertEqual(etat["leader"], "ameesh-agents-1")

    def test_pas_de_primaire(self):
        lecture = json.loads(json.dumps(self.lecture))
        lecture["patroni"] = None
        lecture["erreurs"] = {"patroni": "REST injoignable"}
        alertes, _ = self.analyser(lecture)
        self.assertEqual(self.types(alertes), ["db_no_primary"])
        self.assertIn("REST injoignable", alertes[0]["detail"])
        self.assertEqual(alertes[0]["severity"], "critical")

    def test_bascule_detectee_une_fois(self):
        alertes, etat = self.analyser(etat={"leader": "ameesh-agents-1"})
        self.assertEqual(alertes, [])
        lecture = json.loads(json.dumps(self.lecture))
        lecture["patroni"]["leader"] = "ameesh-agents-2"
        alertes, etat = self.analyser(lecture, etat=etat)
        self.assertEqual(self.types(alertes), ["db_failover"])
        self.assertEqual(etat["leader"], "ameesh-agents-2")
        self.assertIn("dernier_failover", etat)
        alertes, _ = self.analyser(lecture, etat=etat, maintenant=1010)
        self.assertEqual(alertes, [])

    def test_pas_de_bascule_sans_leader(self):
        etat = {"leader": "ameesh-agents-1"}
        lecture = json.loads(json.dumps(self.lecture))
        lecture["patroni"] = {"leader": None, "membres": []}
        alertes, nouvel = self.analyser(lecture, etat=etat)
        self.assertEqual(self.types(alertes), ["db_no_primary"])
        self.assertEqual(nouvel["leader"], "ameesh-agents-1")
        lecture["patroni"]["leader"] = "ameesh-agents-2"
        alertes, _ = self.analyser(lecture, etat=nouvel, maintenant=1010)
        self.assertEqual(self.types(alertes), ["db_failover"])

    def test_retard_de_replication(self):
        lecture = json.loads(json.dumps(self.lecture))
        lecture["patroni"]["membres"][1]["retard"] = 20 * 1024 * 1024
        alertes, _ = self.analyser(lecture)
        self.assertEqual(self.types(alertes), ["db_replication_lag"])
        self.assertEqual(alertes[0]["member"], "ameesh-agents-2")
        self.assertGreater(alertes[0]["value"], alertes[0]["threshold"])

    def test_quorum_etcd(self):
        lecture = json.loads(json.dumps(self.lecture))
        lecture["etcd"][1]["sain"] = False
        self.assertEqual(self.types(self.analyser(lecture)[0]), [])
        lecture["etcd"][0]["sain"] = False
        self.assertEqual(self.types(self.analyser(lecture)[0]), ["db_etcd_no_quorum"])
        lecture["etcd"] = None
        lecture["erreurs"] = {"etcd": "etcdctl absent"}
        alertes, _ = self.analyser(lecture)
        self.assertEqual(self.types(alertes), ["db_etcd_no_quorum"])
        self.assertIn("etcdctl absent", alertes[0]["detail"])

    def test_disque_bas(self):
        lecture = json.loads(json.dumps(self.lecture))
        lecture["disque"]["libre"] = 4 * self.module.GIO
        self.assertEqual(self.types(self.analyser(lecture)[0]), ["db_disk_low"])
        lecture["disque"].update(total=100 * self.module.GIO,
                                 libre=6 * self.module.GIO)
        self.assertEqual(self.types(self.analyser(lecture)[0]), ["db_disk_low"])
        lecture["disque"]["libre"] = 50 * self.module.GIO
        self.assertEqual(self.types(self.analyser(lecture)[0]), [])

    def test_archive_stale(self):
        lecture = json.loads(json.dumps(self.lecture))
        lecture["archivage"] = {"dernier_succes": 50, "dernier_echec": None}
        self.assertEqual(self.types(self.analyser(lecture, maintenant=1000)[0]),
                         ["db_archive_stale"])
        lecture["archivage"] = {"dernier_succes": 900, "dernier_echec": None}
        self.assertEqual(self.types(self.analyser(lecture, maintenant=1000)[0]), [])
        lecture["archivage"] = None
        self.assertEqual(self.types(self.analyser(lecture, maintenant=1000)[0]), [])
        lecture["archivage"] = {"dernier_succes": 1, "dernier_echec": 999}
        alertes = self.analyser(lecture, maintenant=1000)[0]
        self.assertEqual(self.types(alertes), ["db_archive_stale", "db_archive_stale"])
        self.assertNotEqual(alertes[0]["key"], alertes[1]["key"])

    def test_etat_corrompu_ignore_et_sauvegarde(self):
        self.etat.write_text("pas du json")
        self.assertEqual(self.module.charger_etat(self.etat), {})
        erreur = self.module.sauver_etat(self.etat, {"leader": "ameesh-agents-1"})
        self.assertIsNone(erreur)
        self.assertEqual(stat.S_IMODE(self.etat.stat().st_mode), 0o600)
        self.assertEqual(self.module.charger_etat(self.etat)["leader"], "ameesh-agents-1")

    def test_analyser_etcdctl_et_archivage(self):
        texte = ("https://10.78.0.11:2379 is healthy: ok\n"
                 "https://10.78.0.12:2379 is unhealthy: failed to commit proposal\n")
        releve = self.module.analyser_etcdctl(texte)
        self.assertEqual(len(releve), 2)
        self.assertTrue(releve[0]["sain"])
        self.assertFalse(releve[1]["sain"])
        self.assertIsNone(self.module.analyser_etcdctl(""))
        self.assertEqual(self.module.analyser_archivage("on|123.5|"), {
            "dernier_succes": 123.5, "dernier_echec": None})
        self.assertIsNone(self.module.analyser_archivage("off|123.5|"))
        self.assertIsNone(self.module.analyser_archivage(""))

    def test_cli_lecture_et_strict(self):
        fichier = self.tmp / "lecture.json"
        fichier.write_text(json.dumps(self.lecture), encoding="utf-8")
        resultat = _run([sys.executable, str(SANTE), "--lecture", str(fichier),
                         "--etat", str(self.etat), "--maintenant", "1000", "--json"])
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        document = json.loads(resultat.stdout)
        self.assertEqual(document["schema"], "ameesh-db-sante/1")
        self.assertEqual(document["alertes"], [])
        self.assertEqual(document["primaire"], "ameesh-agents-1")
        lecture = json.loads(json.dumps(self.lecture))
        lecture["disque"]["libre"] = 1 * self.module.GIO
        fichier.write_text(json.dumps(lecture), encoding="utf-8")
        strict = _run([sys.executable, str(SANTE), "--lecture", str(fichier),
                       "--etat", str(self.etat), "--maintenant", "1000", "--strict"])
        self.assertEqual(strict.returncode, 1, strict.stderr)

    def test_cli_lecture_invalide(self):
        fichier = self.tmp / "lecture.json"
        fichier.write_text("{")
        resultat = _run([sys.executable, str(SANTE), "--lecture", str(fichier),
                         "--etat", str(self.etat)])
        self.assertEqual(resultat.returncode, 2)
        fichier.write_text("[]")
        resultat = _run([sys.executable, str(SANTE), "--lecture", str(fichier),
                         "--etat", str(self.etat)])
        self.assertEqual(resultat.returncode, 2)

    def test_cli_fichier_alertes(self):
        fichier = self.tmp / "lecture.json"
        lecture = json.loads(json.dumps(self.lecture))
        lecture["disque"]["libre"] = 1 * self.module.GIO
        fichier.write_text(json.dumps(lecture), encoding="utf-8")
        alertes = self.tmp / "alertes.json"
        resultat = _run([sys.executable, str(SANTE), "--lecture", str(fichier),
                         "--etat", str(self.etat), "--maintenant", "1000",
                         "--alerte-fichier", str(alertes)])
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        self.assertEqual(stat.S_IMODE(alertes.stat().st_mode), 0o644)
        document = json.loads(alertes.read_text(encoding="utf-8"))
        self.assertEqual(document["schema"], "ameesh-db-sante/1")
        self.assertEqual([a["type"] for a in document["alertes"]], ["db_disk_low"])

    def test_collecter_avec_doublures(self):
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        etcdctl = bin_ / "etcdctl"
        etcdctl.write_text("#!/bin/sh\n"
                           "printf '%s\\n' 'https://e1 is healthy: ok' "
                           "'https://e2 is healthy: ok'\n")
        etcdctl.chmod(0o755)
        psql = bin_ / "psql"
        psql.write_text("#!/bin/sh\nprintf 'on|900.0|\\n'\n")
        psql.chmod(0o755)
        env = {
            "AMEESH_DB_REST_URL": "https://127.0.0.1:1",
            "AMEESH_DB_ETCD_ENDPOINTS": "https://e1,https://e2",
            "AMEESH_DB_ETCDCTL": str(etcdctl),
            "AMEESH_DB_PSQL": str(psql),
            "AMEESH_DB_DATA_DIR": str(self.tmp),
        }
        lecture = self.module.collecter(env)
        self.assertIsNone(lecture["patroni"])
        self.assertIn("patroni", lecture["erreurs"])
        self.assertEqual(len(lecture["etcd"]), 2)
        self.assertEqual(lecture["archivage"]["dernier_succes"], 900.0)
        self.assertGreater(lecture["disque"]["total"], 0)

    def test_lire_patroni_avec_reponse_simulee(self):
        class Reponse:
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self, _taille):
                return json.dumps({
                    "leader": {"name": "ameesh-agents-1"},
                    "members": [
                        {"name": "ameesh-agents-1", "role": "leader", "state": "running"},
                        {"name": "ameesh-agents-2", "role": "replica",
                         "state": "streaming", "lag": 12},
                    ],
                }).encode("utf-8")

        requete = {}

        def urlopen(request, **_kwargs):
            requete["entete"] = request.get_header("Authorization")
            return Reponse()

        from unittest import mock
        with mock.patch.object(self.module.urllib.request, "urlopen", urlopen), \
                mock.patch.object(self.module, "_contexte_ssl", lambda env: None):
            releve, erreur = self.module.lire_patroni({
                "AMEESH_DB_REST_URL": "https://10.78.0.11:8008",
                "PATRONI_RESTAPI_USERNAME": "patroni",
                "PATRONI_RESTAPI_PASSWORD": "secret-de-test",
            })
        self.assertIsNone(erreur)
        self.assertEqual(releve["leader"], "ameesh-agents-1")
        self.assertEqual(releve["membres"][1]["retard"], 12)
        self.assertTrue(requete["entete"].startswith("Basic "))


class SourcesTest(unittest.TestCase):
    def test_licence_et_fichiers_executables(self):
        for nom in ("generer-configuration.py", "valider-modeles.py", "sante-base.py"):
            texte = (DIR / nom).read_text(encoding="utf-8")
            self.assertIn("SPDX-License-Identifier: AGPL-3.0-only", texte.splitlines()[1])
        for nom in ("generer-tls.sh", "patronictl"):
            texte = (DIR / nom).read_text(encoding="utf-8")
            self.assertIn("SPDX-License-Identifier: AGPL-3.0-only", texte.splitlines()[1])
        for nom in ("generer-configuration.py", "valider-modeles.py", "sante-base.py",
                    "generer-tls.sh", "patronictl"):
            self.assertTrue(os.access(DIR / nom, os.X_OK), nom)

    def test_aucune_reference_a_un_client_reel(self):
        for fichier in DIR.rglob("*"):
            if fichier.is_file() and fichier.suffix != ".pyc":
                texte = fichier.read_text(encoding="utf-8", errors="replace").lower()
                self.assertNotIn("nexlink", texte, fichier)


if __name__ == "__main__":
    unittest.main()
