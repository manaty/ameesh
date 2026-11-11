# SPDX-License-Identifier: AGPL-3.0-only
"""#6902 (EP8.6b) — banc local de la grappe, sans geste réel.

Les contrats hors Docker : topologie du banc, génération des configurations de
#6870, correction constatée d'`etcd3.hosts`, refus de tout geste réel non
configuré, rapport `ameesh-banc-grappe/1`. L'intégration Docker est réservée à
`AMEESH_BANC_DOCKER=1` ; la CI n'a pas de démon Docker.
"""
from __future__ import annotations

import importlib.util
import ipaddress
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIR = ROOT / "deploy" / "serveur" / "patroni" / "banc"
TOPOLOGIE = DIR / "topologie-banc.json"
EXERCER = DIR / "exercer.py"
CORRECTEUR = DIR / "corriger-configuration.py"
GENERATEUR = ROOT / "deploy" / "serveur" / "patroni" / "generer-configuration.py"
TLS = ROOT / "deploy" / "serveur" / "patroni" / "generer-tls.sh"

MESH = (ipaddress.ip_network("10.77.0.0/16"), ipaddress.ip_network("10.78.0.0/16"))


def _env(**extra):
    base = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", tempfile.gettempdir()),
        "TMPDIR": os.environ.get("TMPDIR", tempfile.gettempdir()),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    base.update(extra)
    return base


def _run(args, cwd=None, timeout=180, env=None):
    return subprocess.run(
        [str(a) for a in args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_env(**(env or {})),
    )


def _charger_exercer():
    spec = importlib.util.spec_from_file_location("exercer_6902", EXERCER)
    module = importlib.util.module_from_spec(spec)
    # Les dataclasses du module ont besoin d'être inscrites dans sys.modules
    # pendant l'exécution, sinon ``sys.modules[cls.__module__]`` vaut None.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sans_commentaires(texte):
    """Retire les commentaires de ligne d'un YAML rendu.

    Le gabarit partagé de #6870 cite une adresse du mesh dans un commentaire
    d'exemple ; le banc ne doit pas en contenir dans ses valeurs effectives.
    """
    return "\n".join(
        ligne for ligne in texte.splitlines() if not ligne.lstrip().startswith("#")
    )


class TopologieBancTest(unittest.TestCase):
    def test_trois_noeuds_et_reseau_du_banc(self):
        topologie = json.loads(TOPOLOGIE.read_text(encoding="utf-8"))
        self.assertEqual(len(topologie["noeuds"]), 3)
        reseau = ipaddress.ip_network(topologie["reseau"])
        self.assertTrue(reseau.is_private)
        adresses = [ipaddress.ip_address(n["adresse"]) for n in topologie["noeuds"]]
        for adresse in adresses:
            self.assertIn(adresse, reseau)
        self.assertIn(ipaddress.ip_address(topologie["adresse_pc"]), reseau)

    def test_aucune_adresse_du_mesh(self):
        texte = "\n".join(
            chemin.read_text(encoding="utf-8")
            for chemin in DIR.rglob("*")
            if chemin.is_file() and chemin.suffix in {".py", ".sh", ".json", ".md"}
            and "__pycache__" not in chemin.parts
        )
        for reseau in MESH:
            self.assertNotIn(str(reseau.network_address).rsplit(".", 1)[0] + ".", texte)

    def test_priorites_et_tags(self):
        topologie = json.loads(TOPOLOGIE.read_text(encoding="utf-8"))
        noeuds = {n["nom"]: n for n in topologie["noeuds"]}
        prefere = noeuds[topologie["primaire_prefere"]]
        self.assertFalse(prefere["nofailover"])
        self.assertEqual(prefere["priorite"], max(n["priorite"] for n in noeuds.values()))
        nofailover = [n for n in noeuds.values() if n["nofailover"]]
        self.assertEqual(len(nofailover), 1)
        self.assertTrue(nofailover[0]["nosync"])
        self.assertEqual(nofailover[0]["priorite"], 0)
        self.assertEqual(sum(1 for n in noeuds.values() if not n["nofailover"]), 2)

    def test_image_etce_version_et_empreinte_de_6870(self):
        dockerfile = (DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ETCD_VERSION=3.5.17", dockerfile)
        self.assertIn(
            "eff6ac621d41711085d0f38fab17d8fa3705f6326c3ff11301a1f5a71fc94edd",
            dockerfile,
        )
        self.assertIn("FROM postgres:17-bookworm", dockerfile)


class GenerationConfigurationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = pathlib.Path(tempfile.mkdtemp(prefix="test6902-"))
        self.addCleanup(shutil.rmtree, self._tmp, True)

    def test_generation_puis_correction(self):
        tls = self._tmp / "tls"
        resultat = _run(
            ["bash", TLS, "--sortie", tls, "--topologie", TOPOLOGIE],
            cwd=ROOT / "deploy" / "serveur" / "patroni",
        )
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        secrets = tls / "secrets.env"
        self.assertTrue(secrets.is_file())
        self.assertEqual(stat.S_IMODE(secrets.stat().st_mode), 0o600)

        config = self._tmp / "config"
        resultat = _run(
            [
                sys.executable,
                GENERATEUR,
                "--topologie",
                TOPOLOGIE,
                "--secrets",
                secrets,
                "--sortie",
                config,
                "--tous",
            ],
            cwd=ROOT / "deploy" / "serveur" / "patroni",
        )
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        for nom in ("banc-1", "banc-2", "banc-3"):
            self.assertTrue((config / nom / "patroni.yml").is_file())
            self.assertTrue((config / nom / "etcd.yml").is_file())
            self.assertTrue((config / nom / "patroni.env").is_file())

        patroni = (config / "banc-1" / "patroni.yml").read_text(encoding="utf-8")
        self.assertIn("name: banc-1", patroni)
        self.assertIn("synchronous_mode: true", patroni)
        self.assertIn("172.30.77.11", patroni)
        # Le gabarit de #6870 cite une adresse du mesh en commentaire : seules
        # les valeurs effectives doivent rester sur le réseau du banc.
        self.assertNotIn("10.78.0.", _sans_commentaires(patroni))
        self.assertIn("https://172.30.77.11:2379", patroni)  # constat #6870
        self.assertIn("nofailover: true", (config / "banc-3" / "patroni.yml").read_text())
        self.assertNotIn(
            "nofailover: true", (config / "banc-1" / "patroni.yml").read_text()
        )

        resultat = _run([sys.executable, CORRECTEUR, "--dossier", self._tmp])
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        corrige = (config / "banc-1" / "patroni.yml").read_text(encoding="utf-8")
        self.assertIn("hosts: 172.30.77.11:2379", corrige)
        self.assertNotIn("https://172.30.77.11:2379", corrige)
        constats = json.loads((self._tmp / "constats.json").read_text(encoding="utf-8"))
        self.assertEqual(len(constats), 3)
        self.assertIn("avant", constats[0])
        self.assertIn("apres", constats[0])


class CorrectionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = pathlib.Path(tempfile.mkdtemp(prefix="test6902-corr-"))
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.fichier = self._tmp / "config" / "banc-1" / "patroni.yml"
        self.fichier.parent.mkdir(parents=True)
        self.fichier.write_text(
            "etcd3:\n"
            "  hosts: https://172.30.77.11:2379,https://172.30.77.12:2379\n"
            "  protocol: https\n",
            encoding="utf-8",
        )

    def test_correction_et_constat(self):
        resultat = _run([sys.executable, CORRECTEUR, "--dossier", self._tmp])
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        texte = self.fichier.read_text(encoding="utf-8")
        self.assertIn("  hosts: 172.30.77.11:2379,172.30.77.12:2379", texte)
        self.assertNotIn("https://172.30.77.11", texte)
        constats = json.loads((self._tmp / "constats.json").read_text(encoding="utf-8"))
        self.assertEqual(len(constats), 1)
        self.assertEqual(constats[0]["fichier"], "config/banc-1/patroni.yml")

    def test_deuxieme_passage_idempotent(self):
        self.assertEqual(_run([sys.executable, CORRECTEUR, "--dossier", self._tmp]).returncode, 0)
        avant = self.fichier.read_text(encoding="utf-8")
        self.assertEqual(_run([sys.executable, CORRECTEUR, "--dossier", self._tmp]).returncode, 0)
        self.assertEqual(self.fichier.read_text(encoding="utf-8"), avant)
        constats = json.loads((self._tmp / "constats.json").read_text(encoding="utf-8"))
        self.assertEqual(constats, [])


class SecuriteGesteReelTest(unittest.TestCase):
    def test_refus_grappe_sans_option(self):
        resultat = _run(
            [sys.executable, EXERCER, "--cible", "grappe", "--grappe-config", TOPOLOGIE]
        )
        self.assertEqual(resultat.returncode, 2)
        self.assertIn("refus", resultat.stderr)
        self.assertIn("geste-reel", resultat.stderr)

    def test_refus_geste_reel_sur_le_banc(self):
        resultat = _run([sys.executable, EXERCER, "--cible", "banc", "--geste-reel"])
        self.assertEqual(resultat.returncode, 2)
        self.assertIn("banc", resultat.stderr)

    def test_grappe_refuse_une_action_absente_avant_tout_reseau(self):
        module = _charger_exercer()
        cible = module.GrappeCible(TOPOLOGIE)
        with self.assertRaises(module.Echec):
            cible.geste("perte_noeud", "banc-1")

    def test_grappe_refuse_une_action_inconnue(self):
        module = _charger_exercer()
        configuration = json.loads(TOPOLOGIE.read_text(encoding="utf-8"))
        configuration["actions"] = {"exploser": "true"}
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as flux:
            json.dump(configuration, flux)
            chemin = flux.name
        try:
            with self.assertRaises(module.Erreur):
                module.GrappeCible(pathlib.Path(chemin))
        finally:
            os.unlink(chemin)


class RapportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = pathlib.Path(tempfile.mkdtemp(prefix="test6902-rapport-"))
        self.addCleanup(shutil.rmtree, self._tmp, True)

    def test_autotest_rapport_json_et_markdown(self):
        resultat = _run(
            [sys.executable, EXERCER, "--autotest", "--rapport", self._tmp]
        )
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        rapport = json.loads((self._tmp / "rapport.json").read_text(encoding="utf-8"))
        self.assertEqual(rapport["schema"], "ameesh-banc-grappe/1")
        self.assertEqual(rapport["lot"], 6902)
        self.assertEqual(len(rapport["scenarios"]), 4)
        self.assertIn(rapport["conclusion"]["verdict"], ("ok", "limite", "blocked"))
        markdown = (self._tmp / "rapport.md").read_text(encoding="utf-8")
        self.assertIn("## Synthèse", markdown)
        self.assertIn("perte-primaire", markdown)
        self.assertIn("## Limites", markdown)

    def test_resume_ecritures_ignore_les_lignes_cassees(self):
        module = _charger_exercer()
        fichier = self._tmp / "ecritures.jsonl"
        fichier.write_text(
            '{"id": 1, "noeud": "banc-1", "jeton": "a"}\n'
            "pas du json\n"
            '{"id": 7, "noeud": "banc-2", "jeton": "g"}\n'
            '{"id": 8, "noeud": "banc-2"}\n',
            encoding="utf-8",
        )
        resume = module.resume_ecritures(fichier)
        self.assertEqual(resume["nombre"], 3)
        self.assertEqual(resume["max_id"], 8)
        self.assertEqual(resume["ids"], [1, 7, 8])
        self.assertEqual(resume["paires"], [(1, "a"), (7, "g")])
        # Une ligne confirmée sans jeton ne prouve pas son identité.
        self.assertEqual(resume["sans_jeton"], 1)
        self.assertEqual(resume["noeuds"], ["banc-1", "banc-2"])

    def test_comparer_confirmations_exige_id_et_jeton(self):
        # Témoins négatifs de la revue #39050 : perte après la photographie
        # (id absent) et substitution du même id (jeton différent).
        module = _charger_exercer()
        confirmations = [(1, "a"), (2, "b"), (3, "c")]
        self.assertEqual(
            module.comparer_confirmations(
                confirmations, {1: "a", 2: "b", 3: "c"}
            ),
            ([], []),
        )
        self.assertEqual(
            module.comparer_confirmations(confirmations, {1: "a", 3: "c"}),
            ([2], []),
        )
        self.assertEqual(
            module.comparer_confirmations(confirmations, {1: "a", 2: "X", 3: "c"}),
            ([], [2]),
        )
        self.assertEqual(
            module.comparer_confirmations(confirmations, {}),
            ([1, 2, 3], []),
        )

    def test_ecritures_non_prouvees_lit_id_et_jeton(self):
        module = _charger_exercer()

        class FauxCible:
            nom = "faux"
            noeuds = []
            noeuds_noms = []
            ecrivain_disponible = True
            table_jalon = "t"

            def versions(self):
                return ""

            def accessibilite(self):
                return ""

            def sql(self, nom, requete, timeout=5):
                self.requete = requete
                return "1|a\n3|c"

        cible = FauxCible()
        pilote = module.Pilote(cible, module.Options())
        manquantes, substituees = pilote.ecritures_non_prouvees(
            "banc-1", [(1, "a"), (2, "b"), (3, "c")]
        )
        self.assertEqual(manquantes, [2])
        self.assertEqual(substituees, [])
        self.assertIn("SELECT id, jeton", cible.requete)
        self.assertEqual(pilote.ecritures_non_prouvees("banc-1", []), ([], []))


class ScriptsTest(unittest.TestCase):
    def test_syntaxe_bash(self):
        for nom in ("banc.sh", "entree-noeud.sh", "client-ecriture.sh"):
            resultat = _run(["bash", "-n", DIR / nom])
            self.assertEqual(resultat.returncode, 0, "%s : %s" % (nom, resultat.stderr))

    def test_aide_et_refus_sans_argument(self):
        resultat = _run(["bash", DIR / "banc.sh"])
        self.assertEqual(resultat.returncode, 2)
        self.assertIn("usage: banc.sh", resultat.stderr)
        resultat = _run([sys.executable, EXERCER, "--help"])
        self.assertEqual(resultat.returncode, 0)
        self.assertIn("geste-reel", resultat.stdout)

    def test_scripts_executables(self):
        for nom in (
            "banc.sh",
            "exercer.py",
            "corriger-configuration.py",
            "entree-noeud.sh",
            "client-ecriture.sh",
        ):
            self.assertTrue(os.access(DIR / nom, os.X_OK), "%s non exécutable" % nom)

    def test_donnees_sous_le_dossier_detat(self):
        # Exigence du lot : tout l'état du banc, données comprises, vit sous
        # $BANC_ETAT (par défaut dans $TMPDIR), pas en volumes Docker nommés.
        texte = (DIR / "banc.sh").read_text(encoding="utf-8")
        self.assertNotIn("docker volume create", texte)
        self.assertNotIn("docker volume rm", texte)
        self.assertIn('donnees="$ETAT/donnees/$nom"', texte)
        self.assertIn('etcd="$ETAT/etcd/$nom"', texte)

    def test_marqueur_du_banc_avant_tout_nettoyage(self):
        # B/P1 de la revue : un dossier --etat quelconque ne doit jamais être
        # effacé. Le marqueur (schéma du lot + projet dérivé du chemin) est la
        # seule autorisation de suppression des fichiers.
        def projet_attendu(etat):
            base = re.sub(r"[^a-z0-9-]", "-", etat.name.lower()).strip("-")[:24]
            return "ameesh-banc-" + (base or "banc")

        without = pathlib.Path(tempfile.mkdtemp(prefix="test6902-marqueur-"))
        self.addCleanup(shutil.rmtree, without, True)
        resultat = _run(
            ["bash", DIR / "banc.sh", "verifier-marqueur"],
            env={"BANC_ETAT": str(without)},
        )
        self.assertEqual(resultat.returncode, 1, resultat.stderr)
        (without / "etat.json").write_text(
            json.dumps({"banc": "autre-schema/1", "projet": projet_attendu(without)}),
            encoding="utf-8",
        )
        resultat = _run(
            ["bash", DIR / "banc.sh", "verifier-marqueur"],
            env={"BANC_ETAT": str(without)},
        )
        self.assertEqual(resultat.returncode, 1, resultat.stderr)
        (without / "etat.json").write_text(
            json.dumps(
                {
                    "banc": "ameesh-banc-grappe/1",
                    "lot": 6902,
                    "projet": projet_attendu(without),
                }
            ),
            encoding="utf-8",
        )
        resultat = _run(
            ["bash", DIR / "banc.sh", "verifier-marqueur"],
            env={"BANC_ETAT": str(without)},
        )
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        # Le dossier reste intact : verifier-marqueur est en lecture seule.
        self.assertTrue((without / "etat.json").is_file())

    def test_client_ne_tronque_pas_le_journal(self):
        # B/P2 de la revue : le journal des confirmations doit survivre au
        # redémarrage du poste (le scénario pc-eteint le relit).
        texte = (DIR / "client-ecriture.sh").read_text(encoding="utf-8")
        self.assertNotIn(': > "$SORTIE"', texte)
        self.assertIn('touch -- "$SORTIE"', texte)
        self.assertIn('>> "$SORTIE"', texte)

    def test_pas_de_secret_dans_le_depot(self):
        # ``banc.sh`` lit la variable depuis ``secrets.env`` : une référence
        # ``PATRONI_SUPERUSER_PASSWORD=$(...)`` n'est pas une valeur de secret.
        # On cherche donc une affectation à une valeur réelle (alphanumérique),
        # pas le simple nom de la variable.
        motif = re.compile(r"PATRONI_SUPERUSER_PASSWORD=[A-Za-z0-9]")
        for chemin in DIR.rglob("*"):
            if not chemin.is_file() or chemin.name == "README.md":
                continue
            if "__pycache__" in chemin.parts:
                continue
            texte = chemin.read_text(encoding="utf-8", errors="ignore")
            self.assertIsNone(motif.search(texte), "%s contient un secret" % chemin.name)
            self.assertNotIn("BEGIN RSA PRIVATE KEY", texte)
            self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", texte)


@unittest.skipUnless(
    os.environ.get("AMEESH_BANC_DOCKER") == "1",
    "intégration Docker : poser AMEESH_BANC_DOCKER=1",
)
class IntegrationDockerTest(unittest.TestCase):
    """Montage minimal, hors CI : le rapport complet est lancé par le lot."""

    def test_monter_et_demonter(self):
        etat = pathlib.Path(tempfile.mkdtemp(prefix="test6902-docker-"))
        env = {"BANC_ETAT": str(etat)}
        try:
            resultat = _run(
                ["bash", DIR / "banc.sh", "monter", "--sans-construire"],
                timeout=600,
                env=env,
            )
            self.assertEqual(resultat.returncode, 0, resultat.stderr[-2000:])
            resultat = _run(["bash", DIR / "banc.sh", "etat"], timeout=60, env=env)
            self.assertEqual(resultat.returncode, 0, resultat.stderr)
            self.assertIn("primaire", resultat.stdout)
        finally:
            _run(["bash", DIR / "banc.sh", "demonter"], timeout=120, env=env)
            shutil.rmtree(etat, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
