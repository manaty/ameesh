# SPDX-License-Identifier: AGPL-3.0-only
"""#7962 — le plafonneur du journal json d'un conteneur Docker.

Les tests couvrent le geste lui-même (copie de la fin, gzip, troncature,
rétention), les refus de sûreté (hors DockerRootDir, lien symbolique) et la
propriété qui rend la troncature sûre : un écrivain qui tient le fichier en
O_APPEND continue d'écrire après la troncature, sans trou ni erreur. Aucun
vrai conteneur n'est touché : un faux `docker` rend l'inspection et la racine.
"""

import gzip
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "plafond-journal-docker.py")
SERVICE = os.path.join(ROOT, "deploy", "systemd", "ameesh-pg-journal.service")
TIMER = os.path.join(ROOT, "deploy", "systemd", "ameesh-pg-journal.timer")

FAKE_DOCKER = r'''#!@PYTHON@
import os
import sys
args = sys.argv[1:]
if args[:1] == ["inspect"]:
    sys.stdout.write(open(os.environ["FAKE_INSPECT"]).read())
elif args[:1] == ["info"]:
    print(os.environ["FAKE_ROOT"])
else:
    sys.exit(3)
'''


def charger_module():
    spec = importlib.util.spec_from_file_location("plafond_journal_7962", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PlafondJournalTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="plafond-7962-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = os.path.join(self.dir, "docker")
        self.cid = "a" * 64
        self.cdir = os.path.join(self.root, "containers", self.cid)
        os.makedirs(self.cdir)
        self.journal = os.path.join(self.cdir, self.cid + "-json.log")
        self.etat = os.path.join(self.dir, "state")
        self.inspect = os.path.join(self.dir, "inspect.json")
        self.bin = os.path.join(self.dir, "bin")
        os.mkdir(self.bin)
        self.docker = os.path.join(self.bin, "docker")
        with open(self.docker, "w") as f:
            f.write(FAKE_DOCKER.replace("@PYTHON@", sys.executable))
        os.chmod(self.docker, 0o755)
        self.ecrire_journal()

    def ecrire_journal(self, donnees=None):
        if donnees is None:
            donnees = b"".join(
                b'{"log":"ligne %d\\n","stream":"stderr"}\n' % i for i in range(3))
        with open(self.journal, "wb") as f:
            f.write(donnees)
        return len(donnees)

    def ecrire_inspect(self, log_type="json-file", chemin=None):
        doc = [{
            "Id": self.cid,
            "LogPath": self.journal if chemin is None else chemin,
            "Config": {"Image": "postgres:17"},
            "State": {"Running": True},
            "HostConfig": {"LogConfig": {"Type": log_type, "Config": {}}},
        }]
        with open(self.inspect, "w") as f:
            json.dump(doc, f)

    def run_script(self, *args):
        env = {
            "PATH": self.bin + os.pathsep + "/usr/bin:/bin",
            "HOME": self.dir,
            "FAKE_INSPECT": self.inspect,
            "FAKE_ROOT": self.root,
        }
        return subprocess.run(
            [sys.executable, SCRIPT, "--docker", self.docker,
             "--conteneur", "banc-7962", "--etat-dir", self.etat, *args],
            capture_output=True, text=True, env=env, timeout=30)

    def archives(self):
        if not os.path.isdir(self.etat):
            return []
        return sorted(n for n in os.listdir(self.etat)
                      if n.startswith("banc-7962-") and n.endswith(".log.gz"))

    def test_etat_est_lecture_seule(self):
        self.ecrire_inspect()
        avant = os.path.getsize(self.journal)
        proc = self.run_script("--etat", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        etat = json.loads(proc.stdout)
        self.assertEqual(etat["schema"], "ameesh-journal-conteneur/1")
        self.assertEqual(etat["octets"], avant)
        self.assertEqual(etat["seuil"], 128 * 1024 * 1024)
        self.assertEqual(etat["journal"], self.journal)
        self.assertFalse(os.path.exists(self.etat),
                         "--etat ne crée même pas le répertoire d'état")

    def test_sous_le_seuil_ne_touche_rien(self):
        self.ecrire_inspect()
        avant = os.path.getsize(self.journal)
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(os.path.getsize(self.journal), avant)
        self.assertEqual(self.archives(), [])
        self.assertEqual(stat.S_IMODE(os.stat(self.etat).st_mode), 0o700)

    def test_au_dessus_archive_la_fin_et_tronque(self):
        donnees = b"".join(b'{"log":"ligne %d\\n"}\n' % i for i in range(30))
        avant = self.ecrire_journal(donnees)
        self.ecrire_inspect()
        proc = self.run_script("--seuil", "100", "--garder", "200", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        resultat = json.loads(proc.stdout)
        self.assertEqual(resultat["action"], "tronque")
        self.assertEqual(resultat["octets_avant"], avant)
        self.assertEqual(os.path.getsize(self.journal), 0)
        arcs = self.archives()
        self.assertEqual(len(arcs), 1)
        with gzip.open(os.path.join(self.etat, arcs[0]), "rb") as f:
            fin = f.read()
        self.assertTrue(fin.endswith(b'"}\n'))
        self.assertEqual(b"".join(ligne + b"\n" for ligne in fin.splitlines()),
                         fin)
        for ligne in fin.splitlines():
            json.loads(ligne)
        self.assertTrue(fin.startswith(b'{"log":'))

    def test_copie_partielle_commence_apres_le_saut_de_ligne(self):
        debut = b"X" * 100 + b"\n"
        ligne1 = b'{"log":"a"}\n'
        ligne2 = b'{"log":"b"}\n'
        self.ecrire_journal(debut + ligne1 + ligne2)
        self.ecrire_inspect()
        garder = str(len(ligne2) + 10)  # commence au milieu de ligne1
        proc = self.run_script("--seuil", "10", "--garder", garder, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        arcs = self.archives()
        self.assertEqual(len(arcs), 1)
        with gzip.open(os.path.join(self.etat, arcs[0]), "rb") as f:
            self.assertEqual(f.read(), ligne2)

    def test_retention_des_archives(self):
        os.makedirs(self.etat, mode=0o700)
        for i in range(10):
            chemin = os.path.join(self.etat, "banc-7962-2020010%dT000000Z.log.gz" % i)
            with open(chemin, "wb") as f:
                f.write(b"x")
            os.utime(chemin, (1000 + i, 1000 + i))
        self.ecrire_journal(b"y" * 500)
        self.ecrire_inspect()
        proc = self.run_script("--seuil", "10", "--garder", "100", "--archives", "3")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        arcs = self.archives()
        self.assertEqual(len(arcs), 3)
        self.assertIn("banc-7962-20200109T000000Z.log.gz", arcs)
        self.assertIn("banc-7962-20200108T000000Z.log.gz", arcs)

    def test_refus_hors_de_docker_root(self):
        ailleurs = os.path.join(self.dir, "ailleurs-json.log")
        with open(ailleurs, "wb") as f:
            f.write(b"x" * 200)
        self.ecrire_inspect(chemin=ailleurs)
        proc = self.run_script("--seuil", "10")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("hors de", proc.stderr)
        self.assertEqual(os.path.getsize(ailleurs), 200)

    def test_refus_lien_symbolique(self):
        cible = os.path.join(self.dir, "cible-json.log")
        with open(cible, "wb") as f:
            f.write(b"x" * 200)
        lien = os.path.join(self.cdir, "lien-json.log")
        os.symlink(cible, lien)
        self.ecrire_inspect(chemin=lien)
        proc = self.run_script("--seuil", "10")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("symbolique", proc.stderr)
        self.assertEqual(os.path.getsize(cible), 200)

    def test_pilote_non_json_file_est_ignore(self):
        self.ecrire_inspect(log_type="journald")
        avant = os.path.getsize(self.journal)
        proc = self.run_script("--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["action"], "ignore")
        self.assertEqual(os.path.getsize(self.journal), avant)

    def test_dry_run_ne_touche_rien(self):
        self.ecrire_journal(b"z" * 500)
        self.ecrire_inspect()
        proc = self.run_script("--seuil", "10", "--dry-run", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["action"], "tronque")
        self.assertEqual(os.path.getsize(self.journal), 500)
        self.assertEqual(self.archives(), [])

    def test_ecrivain_o_append_survit_a_la_troncature(self):
        """La propriété qui rend le copytruncate sûr (preuve AG2 20/11)."""
        self.ecrire_journal(b"q" * 500)
        module = charger_module()
        os.makedirs(self.etat, mode=0o700)
        fd = os.open(self.journal, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(fd, b'{"log":"avant"}\n')
            resultat = module.plafonner(self.journal, 100, 200, self.etat,
                                        "banc-7962", 8)
            self.assertEqual(resultat["action"], "tronque")
            os.write(fd, b'{"log":"apres"}\n')
        finally:
            os.close(fd)
        with open(self.journal, "rb") as f:
            self.assertEqual(f.read(), b'{"log":"apres"}\n')

    def test_unites_utilisateur(self):
        with open(SERVICE) as f:
            service = f.read()
        with open(TIMER) as f:
            timer = f.read()
        self.assertIn("DOCKER_HOST=unix://%t/docker.sock", service)
        self.assertIn("plafond-journal-docker.py --conteneur ameesh-pg", service)
        self.assertIn("NoNewPrivileges=yes", service)
        self.assertIn("OnUnitInactiveSec=10min", timer)
        self.assertIn("WantedBy=timers.target", timer)


if __name__ == "__main__":
    unittest.main()
