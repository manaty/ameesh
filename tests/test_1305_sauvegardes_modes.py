# SPDX-License-Identifier: AGPL-3.0-only
"""#1305/#1309 — les dumps de sauvegarde ne sont lisibles que par ameesh.

Le vrai `deploy/serveur/sauvegarder-base.sh` est exécuté ; la copie et la
publication passent par le canal privé L232b (`sauvegarder-prive.py`), dont le
staging 0700 protège le fichier avant sa publication par lien dur : le dump
publié ressort en 0600 quel que soit le umask de l'appelant (#1305) ou une ACL
par défaut du répertoire (#1309), et un fichier existant n'est jamais écrasé.

Le faux `pg_dump` du PATH est celui que le helper lance ; il note le mode, le
chemin et le répertoire qu'il voit à l'ouverture. Le répertoire des dumps est
redirigé par `AMEESH_SAUVEGARDES_DIR`, la machine et l'horloge du nom de dump
sont figées par de faux `hostname`/`date` du PATH ; le `date +%s` de la
rétention garde l'horloge réelle, puisque l'âge des dumps se mesure sur les
mtime réels. Aucune base, aucun réseau.

La part psql de la CI n'a pas psycopg : le helper privé n'y est pas couvert
(même règle que tests/test_l232b_private_backup.py), ces témoins y sont sautés.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "deploy", "serveur", "sauvegarder-base.sh")
HELPER = os.path.join(ROOT, "deploy", "mise-a-jour", "sauvegarder-prive.py")
RETENTION = os.path.join(ROOT, "deploy", "mise-a-jour", "retention-privee.py")
HAVE_PSYCOPG = importlib.util.find_spec("psycopg") is not None

# Encodage binaire d'une ACL POSIX par défaut (xattr system.posix_acl_default,
# noyau Linux) : en-tête version 2, puis des entrées (tag, permissions, id).
ACL_VERSION = 0x0002
ACL_USER_OBJ, ACL_GROUP_OBJ, ACL_MASK, ACL_OTHER = 0x01, 0x04, 0x10, 0x20
ACL_DEFAUT_PERMISSIVE = struct.pack("<I", ACL_VERSION) + b"".join(
    struct.pack("<HHI", tag, 0o7, 0xFFFFFFFF)
    for tag in (ACL_USER_OBJ, ACL_GROUP_OBJ, ACL_MASK, ACL_OTHER))

FAKE_PG_DUMP = '''#!/usr/bin/env python3
"""Faux pg_dump : note le mode, le chemin et le répertoire vus à l'ouverture.

Le helper L232b le lance avec `--file` dans son staging privé ; le mode observé
est celui du staging, pas celui du répertoire des dumps.
"""
import os
import sys

args = sys.argv[1:]
if args == ["--version"]:  # #5189 : la garde des majors interroge ce binaire
    print("pg_dump (PostgreSQL) 17.11")
    sys.exit(0)
if "-f" in args:
    chemin = args[args.index("-f") + 1]
else:
    chemin = args[args.index("--file") + 1]
parent = os.path.dirname(chemin)
etat = os.environ["FAKE_STATE"]
try:
    mode = "%o" % (os.stat(chemin).st_mode & 0o7777)
except FileNotFoundError:
    mode = "absent"
with open(os.path.join(etat, "mode-ouverture"), "w") as fh:
    fh.write(mode)
with open(os.path.join(etat, "chemin-ouverture"), "w") as fh:
    fh.write(chemin)
with open(os.path.join(etat, "parent-ouverture"), "w") as fh:
    fh.write("%o" % (os.stat(parent).st_mode & 0o7777))
with open(chemin, "w") as fh:  # tronque sans changer le mode, comme pg_dump
    fh.write("dump %s\\n" % chemin)
'''

FAKE_PG_RESTORE = '''#!/bin/sh
[ "$1" = --version ] && echo "pg_restore (PostgreSQL) 17.11"
exit 0
'''

# #5189 : `dump` lit le major du serveur source avant pg_dump ; la configuration
# de test ne vise aucun serveur, le lanceur fixe ce seul major (17) et exécute
# le corps du helper du dépôt.
LANCEUR = '''import importlib.util
import sys

spec = importlib.util.spec_from_file_location("sauvegarder_prive", %r)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.source_server_major = lambda parameters: 17
raise SystemExit(module.main())
'''

FAKE_CURL = '''#!/bin/sh
exit 0
'''

# Le nom du dump dépend de la machine et de l'horloge : on les fige dans le
# PATH du script. `date +%s` (rétention) suit l'horloge réelle, sinon l'âge
# mesuré sur les mtime ne correspondrait plus.
FAKE_HOSTNAME = '''#!/bin/sh
echo hote-test
'''

FAKE_DATE = '''#!/bin/sh
case "$1" in
    +%F-%H%M%S) echo 2026-01-02-030000 ;;
    +%F) echo 2026-01-02 ;;
    *) exec /bin/date "$@" ;;
esac
'''

HOTE = "hote-test"
JOUR = "2026-01-02"
HEURE = "030000"


@unittest.skipUnless(HAVE_PSYCOPG, "psycopg absent (part psql) : canal privé non couvert")
class SauvegardesPriveesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-1305-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dumps = os.path.join(self.tmp, "sauvegardes")
        self.state = os.path.join(self.tmp, "state")
        fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.state)
        os.makedirs(fakebin, mode=0o755)
        for nom, contenu in (("pg_dump", FAKE_PG_DUMP),
                             ("pg_restore", FAKE_PG_RESTORE),
                             ("curl", FAKE_CURL), ("hostname", FAKE_HOSTNAME),
                             ("date", FAKE_DATE)):
            chemin = os.path.join(fakebin, nom)
            with open(chemin, "w", encoding="utf-8") as fh:
                fh.write(contenu)
            os.chmod(chemin, 0o755)
        self.config = os.path.join(self.tmp, "config.json")
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump({"dsn": "postgresql://ameesh@127.0.0.1:5432/ameesh"}, fh)
        # Le script est rejoué avec le helper et la rétention du dépôt, la
        # configuration de test et les chemins système absents ; le corps
        # exécuté reste celui du dépôt.
        self.lanceur = os.path.join(self.tmp, "lanceur.py")
        with open(self.lanceur, "w", encoding="utf-8") as fh:
            fh.write(LANCEUR % HELPER)
        self.script = os.path.join(self.tmp, "sauvegarder-base.sh")
        with open(self.script, "w", encoding="utf-8") as fh:
            fh.write(self.script_adapte())
        self.env = dict(os.environ, FAKE_STATE=self.state,
                        AMEESH_SAUVEGARDES_DIR=self.dumps,
                        PATH=fakebin + os.pathsep + os.environ.get("PATH", ""))
        # umask permissif de l'appelant : le script doit imposer le sien
        ancien = os.umask(0o022)
        self.addCleanup(os.umask, ancien)

    def script_adapte(self):
        with open(SCRIPT, encoding="utf-8") as fh:
            texte = fh.read()
        return (texte
                .replace("python=/opt/ameesh/venv/bin/python",
                         "python=" + shlex.quote(sys.executable))
                .replace("helper=/usr/local/sbin/ameesh-sauvegarder-prive.py",
                         "helper=" + shlex.quote(self.lanceur))
                .replace("/usr/local/sbin/ameesh-retention-privee.py",
                         shlex.quote(RETENTION))
                .replace("/etc/ameesh/sauvegarde.env",
                         os.path.join(self.tmp, "absent-storage.env"))
                .replace("/etc/ameesh/retention-provider.json",
                         os.path.join(self.tmp, "absent-provider.json"))
                .replace("/home/ameesh/.config/ameesh/config.json", self.config))

    def run_script(self):
        return subprocess.run(["/bin/sh", self.script], capture_output=True,
                              text=True, env=self.env, timeout=120)

    def mode(self, chemin):
        return stat.S_IMODE(os.stat(chemin).st_mode)

    def dump_du_jour(self):
        return os.path.join(self.dumps,
                            "ameesh-%s-%s-%s.dump" % (HOTE, JOUR, HEURE))

    def dump_herite(self):
        """Nom quotidien d'une version antérieure (sans heure)."""
        return os.path.join(self.dumps, "ameesh-%s-%s.dump" % (HOTE, JOUR))

    def dump_ancien(self, nom, contenu, age_jours=0):
        chemin = os.path.join(self.dumps, nom)
        with open(chemin, "w", encoding="utf-8") as fh:
            fh.write(contenu)
        os.chmod(chemin, 0o644)
        if age_jours:
            vieux = os.stat(chemin).st_mtime - age_jours * 86400
            os.utime(chemin, (vieux, vieux))
        return chemin

    def etat(self, nom):
        with open(os.path.join(self.state, nom), encoding="utf-8") as fh:
            return fh.read()

    def mode_vu_par_pg_dump(self):
        return self.etat("mode-ouverture")

    def test_repertoire_cree_prive_et_dump_0600(self):
        self.assertFalse(os.path.exists(self.dumps))
        fait = self.run_script()
        self.assertEqual(fait.returncode, 0, fait.stderr)
        self.assertEqual(self.mode(self.dumps), 0o700)
        self.assertEqual(self.mode(self.dump_du_jour()), 0o600)
        # La copie s'écrit dans le staging privé du helper (0700), pas dans le
        # répertoire des dumps : c'est lui qui protège le fichier avant la
        # publication par lien dur.
        self.assertEqual(self.etat("parent-ouverture"), "700")
        self.assertIn(os.sep + ".backup-", self.etat("chemin-ouverture"))
        self.assertEqual(self.mode_vu_par_pg_dump(), "absent")

    def test_repertoire_preexistant_0755_restreint(self):
        os.makedirs(self.dumps, mode=0o755)
        os.chmod(self.dumps, 0o755)
        fait = self.run_script()
        self.assertEqual(fait.returncode, 0, fait.stderr)
        self.assertEqual(self.mode(self.dumps), 0o700)
        self.assertEqual(self.mode(self.dump_du_jour()), 0o600)

    def test_dump_herite_0644_normalise_sans_reecriture(self):
        """Un dump du jour d'une version antérieure (0644) devient privé sans
        être réécrit ; le nouveau dump horodaté est publié à côté."""
        os.makedirs(self.dumps)
        chemin = self.dump_herite()
        with open(chemin, "w", encoding="utf-8") as fh:
            fh.write("ancien dump du jour\n")
        os.chmod(chemin, 0o644)
        fait = self.run_script()
        self.assertEqual(fait.returncode, 0, fait.stderr)
        self.assertEqual(self.mode(chemin), 0o600)
        with open(chemin, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "ancien dump du jour\n")
        self.assertEqual(self.mode(self.dump_du_jour()), 0o600)

    def test_destination_existante_refusee_contenu_intact(self):
        """Le canal privé ne remplace jamais un dump existant : le contenu
        reste intact, la copie échoue et la rétention ne démarre pas."""
        os.makedirs(self.dumps)
        chemin = self.dump_du_jour()
        with open(chemin, "w", encoding="utf-8") as fh:
            fh.write("ancien contenu\n")
        os.chmod(chemin, 0o644)
        fait = self.run_script()
        self.assertNotEqual(fait.returncode, 0)
        with open(chemin, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "ancien contenu\n")
        # La normalisation des modes précède la copie : le refus n'empêche pas
        # le fichier existant de devenir privé, et rien d'autre n'est laissé.
        self.assertEqual(self.mode(chemin), 0o600)
        self.assertEqual(os.listdir(self.dumps), [os.path.basename(chemin)])

    def test_dumps_anciens_normalises_sans_reecriture(self):
        os.makedirs(self.dumps)
        chemin = self.dump_ancien("ameesh-vieux-2020-01-01.dump", "intact\n")
        fait = self.run_script()
        self.assertEqual(fait.returncode, 0, fait.stderr)
        self.assertEqual(self.mode(chemin), 0o600)
        with open(chemin, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "intact\n")

    def test_dump_de_plus_de_14_jours_supprime(self):
        os.makedirs(self.dumps)
        chemin = self.dump_ancien("ameesh-vieux-2019-12-31.dump", "vieux\n",
                                  age_jours=20)
        fait = self.run_script()
        self.assertEqual(fait.returncode, 0, fait.stderr)
        self.assertFalse(os.path.exists(chemin))

    def poser_acl_defaut(self, repertoire):
        """Pose une ACL par défaut permissive sur le répertoire (#1309).

        `setfacl` sert quand il est installé ; sinon l'attribut système est
        posé directement. Rend False quand ni l'outil ni le système de fichiers
        ne peuvent la porter : le témoin est alors sauté.
        """
        setfacl = shutil.which("setfacl")
        if setfacl is not None:
            fait = subprocess.run(
                [setfacl, "-d", "-m", "u::rwx,g::rwx,o::rwx,m::rwx",
                 repertoire], capture_output=True, text=True)
            return fait.returncode == 0
        try:
            os.setxattr(repertoire, "system.posix_acl_default",
                        ACL_DEFAUT_PERMISSIVE)
        except (AttributeError, OSError):
            return False
        return True

    def test_acl_par_defaut_neutre_le_dump_nouveau(self):
        """Un dump publié reste privé malgré l'ACL du répertoire."""
        os.makedirs(self.dumps)
        if not self.poser_acl_defaut(self.dumps):
            self.skipTest("ACL POSIX par défaut indisponible sur ce système")
        # Témoin du témoin : sous umask 077, un fichier nouveau dans ce
        # répertoire suit l'ACL, pas le umask. S'il ressort en 0600, le système
        # de fichiers ignore l'ACL et le test ne prouverait rien.
        temoin = os.path.join(self.dumps, "temoin-acl")
        ancien = os.umask(0o077)
        try:
            descripteur = os.open(temoin, os.O_CREAT | os.O_WRONLY, 0o666)
            os.close(descripteur)
        finally:
            os.umask(ancien)
        if self.mode(temoin) == 0o600:
            self.skipTest("le système de fichiers n'applique pas l'ACL par "
                          "défaut")
        fait = self.run_script()
        self.assertEqual(fait.returncode, 0, fait.stderr)
        # Le staging 0700 masque l'ACL du répertoire, et la publication par
        # lien dur garde le mode privé du fichier staged.
        self.assertEqual(self.etat("parent-ouverture"), "700")
        self.assertEqual(self.mode(self.dump_du_jour()), 0o600)


if __name__ == "__main__":
    unittest.main()
