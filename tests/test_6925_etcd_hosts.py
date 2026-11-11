# SPDX-License-Identifier: AGPL-3.0-only
"""#6925 (EP8.4b correctif) — etcd3.hosts en hôte:port pour Patroni 4.x.

Le banc #6902 a constaté que `generer-configuration.py` (#6870) rendait
« https://hôte:2379 » dans `etcd3.hosts`. Patroni 4.x attend « hôte:port » et
porte le schéma dans `etcd3.protocol: https` ; avec le schéma, il prend
l'hôte pour une IPv6 et refuse de démarrer (`ValueError: Invalid IPv6 URL`).

Ce témoin rejoue le banc sans sa correction locale : la configuration générée
doit être acceptée telle quelle, et `--verifier` doit refuser la forme avec
schéma. La validation Patroni n'est exécutée que si le module est présent
(canon §3 : un test non concluant ne retient rien).
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover - le venv de contrats le fournit
    yaml = None

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIR = ROOT / "deploy" / "serveur" / "patroni"
GENERATEUR = DIR / "generer-configuration.py"
VALIDATEUR = DIR / "valider-modeles.py"
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


def _run(args, timeout=120):
    return subprocess.run(args, capture_output=True, text=True,
                          env=_env(), timeout=timeout)


def _charger(chemin, nom):
    spec = importlib.util.spec_from_file_location(nom, chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _yaml(chemin):
    if yaml is None:
        raise unittest.SkipTest("PyYAML absent")
    return yaml.safe_load(pathlib.Path(chemin).read_text(encoding="utf-8"))


class EtcdHostsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="t6925-etcd-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.topologie = json.loads(TOPOLOGIE.read_text(encoding="utf-8"))
        self.topologie_fichier = self.tmp / "topologie.json"
        self.topologie_fichier.write_text(
            json.dumps(self.topologie, ensure_ascii=False), encoding="utf-8")
        self.secrets = self.tmp / "secrets.env"
        self.secrets.write_text(SECRETS, encoding="utf-8")
        self.secrets.chmod(0o600)
        self.sortie = self.tmp / "config"

    def generer(self, *options):
        return _run([sys.executable, str(GENERATEUR),
                     "--topologie", str(self.topologie_fichier),
                     "--secrets", str(self.secrets),
                     "--sortie", str(self.sortie), *options])

    def _ordre(self, noeud):
        return [noeud] + [n for n in self.topologie["noeuds"]
                          if n["nom"] != noeud["nom"]]

    def _fichiers(self):
        return {
            noeud["nom"]: {
                "patroni": self.sortie / noeud["nom"] / "patroni.yml",
                "etcd": self.sortie / noeud["nom"] / "etcd.yml",
                "env": self.sortie / noeud["nom"] / "patroni.env",
            }
            for noeud in self.topologie["noeuds"]
        }

    @unittest.skipIf(yaml is None, "PyYAML absent")
    def test_hosts_en_hote_port_et_protocole_separe(self):
        resultat = self.generer("--tous", "--verifier")
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        ports = self.topologie["ports"]
        for noeud in self.topologie["noeuds"]:
            patroni = _yaml(self.sortie / noeud["nom"] / "patroni.yml")
            attendus = ["%s:%d" % (n["adresse"], ports["etcd_client"])
                        for n in self._ordre(noeud)]
            hotes = patroni["etcd3"]["hosts"]
            self.assertIsInstance(hotes, str)
            self.assertEqual(hotes.split(","), attendus)
            self.assertNotIn("://", hotes)
            self.assertEqual(patroni["etcd3"]["protocol"], "https")
            self.assertEqual(patroni["etcd3"]["cacert"],
                             self.topologie["chemins"]["ca"])
            self.assertEqual(patroni["etcd3"]["cert"],
                             self.topologie["chemins"]["patroni_tls"] + "/etcd-client.crt")
            self.assertEqual(patroni["etcd3"]["key"],
                             self.topologie["chemins"]["patroni_tls"] + "/etcd-client.key")

            env = {}
            for ligne in (self.sortie / noeud["nom"] / "patroni.env").read_text(
                    encoding="utf-8").splitlines():
                if ligne and not ligne.startswith("#") and "=" in ligne:
                    cle, valeur = ligne.split("=", 1)
                    env[cle] = valeur
            # etcdctl (santé) attend des URL complètes : le schéma y reste.
            self.assertEqual(
                env["AMEESH_DB_ETCD_ENDPOINTS"].split(","),
                ["https://%s:%d" % (n["adresse"], ports["etcd_client"])
                 for n in self._ordre(noeud)])

    @unittest.skipIf(yaml is None, "PyYAML absent")
    @unittest.skipUnless(importlib.util.find_spec("patroni"), "module Patroni absent")
    def test_patroni_4_accepte_la_configuration_generee(self):
        self.assertEqual(self.generer("--tous").returncode, 0)
        fichiers = [str(self.sortie / n["nom"] / "patroni.yml")
                    for n in self.topologie["noeuds"]]
        resultat = _run([sys.executable, str(VALIDATEUR)] + fichiers)
        self.assertEqual(resultat.returncode, 0, resultat.stderr)

        # Même chemin que patroni.dcs.etcd.AbstractEtcd.get_etcd_client : il
        # découpe chaque hôte puis reconstruit l'URL avec le protocole. Avec
        # « https://hôte:2379 », uri() rend « https://[https://hôte]:2379 » et
        # urlsplit() lève ValueError: Invalid IPv6 URL, comme au banc #6902.
        from patroni.utils import split_host_port, uri
        from urllib.parse import urlsplit

        ports = self.topologie["ports"]
        for noeud in self.topologie["noeuds"]:
            patroni = _yaml(self.sortie / noeud["nom"] / "patroni.yml")
            for hote in patroni["etcd3"]["hosts"].split(","):
                adresse, port = split_host_port(hote, ports["etcd_client"])
                self.assertEqual(port, ports["etcd_client"])
                url = uri(patroni["etcd3"]["protocol"], (adresse, port))
                self.assertEqual(url, "https://%s:%d" % (adresse, ports["etcd_client"]))
                self.assertEqual(urlsplit(url).hostname, adresse)

    def test_verificateur_refuse_le_schema_dans_hosts(self):
        module = _charger(GENERATEUR, "generer_configuration_6925")
        topologie = module.charger_topologie(self.topologie_fichier)
        secrets = module.charger_secrets(self.secrets)
        self.assertEqual(self.generer("--tous").returncode, 0)
        fichiers = self._fichiers()
        patroni = fichiers["ameesh-agents-1"]["patroni"]
        texte = patroni.read_text(encoding="utf-8")
        self.assertIn("  hosts: 10.78.0.11:2379,", texte)
        patroni.write_text(
            texte.replace("  hosts: 10.78.0.11:2379,",
                          "  hosts: https://10.78.0.11:2379,"),
            encoding="utf-8")
        with self.assertRaisesRegex(module.Erreur, "hôte:port"):
            module.verifier_structure(topologie, secrets, fichiers)

    def test_banc_6902_ne_trouve_plus_rien_a_corriger(self):
        """Le banc applique cette règle à ses copies : plus aucun constat."""
        self.assertEqual(self.generer("--tous").returncode, 0)
        motif = re.compile(r"^  hosts: (\S.*)$", re.M)
        constats = []
        for fichier in sorted(self.sortie.glob("*/patroni.yml")):
            trouve = motif.search(fichier.read_text(encoding="utf-8"))
            self.assertIsNotNone(trouve, fichier)
            constats.extend(hote for hote in trouve.group(1).split(",")
                            if "://" in hote)
        self.assertEqual(constats, [],
                         "le banc #6902 corrigerait encore : %r" % constats)


if __name__ == "__main__":
    unittest.main()
