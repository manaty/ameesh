# SPDX-License-Identifier: AGPL-3.0-only
"""#1305 — les dumps de sauvegarde ne sont lisibles que par ameesh.

Le vrai `deploy/serveur/sauvegarder-base.sh` est exécuté, avec un faux helper
privé (`AMEESH_SAUVEGARDER_PRIVE`, #364/#1484) qui se comporte comme le vrai
sur le point qui compte : il écrit le dump sans changer le mode du fichier et
note le mode qu'il voit à l'ouverture. Un faux helper de rétention
(`AMEESH_RETENTION_PRIVEE`) note les commandes reçues : le script ne supprime
jamais un dump lui-même — le `find -delete` brut de 14 jours a disparu avec la
rétention par catalogue privé — il passe par `expire-unregistered` puis
`prune`, qui seuls expirent un candidat non inscrit et préservent les
ensembles inscrits au catalogue.

Le répertoire des dumps est redirigé par `AMEESH_SAUVEGARDES_DIR`, positionnée
ici seulement (le service systemd ne la connaît pas) ; la machine et la date du
dump du jour sont figées par de faux `hostname`/`date` du PATH. Aucune base,
aucun réseau.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "deploy", "serveur", "sauvegarder-base.sh")

# Encodage binaire d'une ACL POSIX par défaut (xattr system.posix_acl_default,
# noyau Linux) : en-tête version 2, puis des entrées (tag, permissions, id).
ACL_VERSION = 0x0002
ACL_USER_OBJ, ACL_GROUP_OBJ, ACL_MASK, ACL_OTHER = 0x01, 0x04, 0x10, 0x20
ACL_DEFAUT_PERMISSIVE = struct.pack("<I", ACL_VERSION) + b"".join(
    struct.pack("<HHI", tag, 0o7, 0xFFFFFFFF)
    for tag in (ACL_USER_OBJ, ACL_GROUP_OBJ, ACL_MASK, ACL_OTHER))

# Faux helper privé : le vrai lance `pg_dump` ; celui-ci note le mode vu à
# l'ouverture, puis tronque le fichier sans changer son mode, comme pg_dump.
FAKE_HELPER = '''#!/usr/bin/env python3
"""Faux helper privé : note le mode vu à l'ouverture, puis écrit le dump."""
import json
import os
import sys

state = os.environ["FAKE_STATE"]
argv = sys.argv[1:]
with open(os.path.join(state, "helper-argv"), "a") as fh:
    fh.write(json.dumps(argv) + "\\n")
if argv and argv[0] == "dump":
    chemin = argv[argv.index("--output") + 1]
    try:
        mode = "%o" % (os.stat(chemin).st_mode & 0o7777)
    except FileNotFoundError:
        mode = "absent"
    with open(os.path.join(state, "mode-ouverture"), "w") as fh:
        fh.write(mode)
    with open(chemin, "w") as fh:  # tronque sans changer le mode
        fh.write("dump %s\\n" % chemin)
'''

# Faux helper de rétention : il note ses arguments et ne supprime rien, pour
# que le témoin voie ce que le script demande au catalogue privé.
FAKE_RETENTION = '''#!/usr/bin/env python3
"""Faux helper de rétention : note la commande reçue, ne supprime rien."""
import json
import os
import sys

with open(os.path.join(os.environ["FAKE_STATE"], "retention-argv"), "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
'''

# Le nom du dump du jour dépend de la machine et de l'horloge : on les fige
# dans le PATH du script, pour que le test ne dépende ni de l'une ni de l'autre.
FAKE_HOSTNAME = '''#!/bin/sh
echo hote-test
'''

FAKE_DATE = '''#!/bin/sh
echo 2026-01-02
'''

HOTE = "hote-test"
JOUR = "2026-01-02"


class SauvegardesPriveesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-1305-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dumps = os.path.join(self.tmp, "sauvegardes")
        self.state = os.path.join(self.tmp, "state")
        fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.state)
        os.makedirs(fakebin, mode=0o755)
        for nom, contenu in (("hostname", FAKE_HOSTNAME), ("date", FAKE_DATE)):
            chemin = os.path.join(fakebin, nom)
            with open(chemin, "w", encoding="utf-8") as fh:
                fh.write(contenu)
            os.chmod(chemin, 0o755)
        self.helper = os.path.join(self.tmp, "faux-helper-prive.py")
        self.retention = os.path.join(self.tmp, "faux-helper-retention.py")
        for chemin, contenu in ((self.helper, FAKE_HELPER),
                                (self.retention, FAKE_RETENTION)):
            with open(chemin, "w", encoding="utf-8") as fh:
                fh.write(contenu)
            os.chmod(chemin, 0o755)
        self.env = dict(os.environ, FAKE_STATE=self.state,
                        AMEESH_SAUVEGARDES_DIR=self.dumps,
                        AMEESH_PYTHON=sys.executable,
                        AMEESH_SAUVEGARDER_PRIVE=self.helper,
                        AMEESH_RETENTION_PRIVEE=self.retention,
                        PATH=fakebin + os.pathsep + os.environ.get("PATH", ""))
        # umask permissif de l'appelant : le script doit imposer le sien
        ancien = os.umask(0o022)
        self.addCleanup(os.umask, ancien)

    def run_script(self):
        return subprocess.run(["/bin/sh", SCRIPT], capture_output=True,
                              text=True, env=self.env, timeout=60)

    def mode(self, chemin):
        return stat.S_IMODE(os.stat(chemin).st_mode)

    def dump_du_jour(self):
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

    def mode_vu_par_le_helper(self):
        with open(os.path.join(self.state, "mode-ouverture"),
                  encoding="utf-8") as fh:
            return fh.read()

    def helper_argv(self):
        with open(os.path.join(self.state, "helper-argv"),
                  encoding="utf-8") as fh:
            return [json.loads(ligne) for ligne in fh if ligne.strip()]

    def retention_argv(self):
        with open(os.path.join(self.state, "retention-argv"),
                  encoding="utf-8") as fh:
            return [json.loads(ligne) for ligne in fh if ligne.strip()]

    def test_repertoire_cree_prive_et_dump_0600(self):
        self.assertFalse(os.path.exists(self.dumps))
        fait = self.run_script()
        self.assertEqual(fait.returncode, 0, fait.stderr)
        self.assertEqual(self.mode(self.dumps), 0o700)
        self.assertEqual(self.mode(self.dump_du_jour()), 0o600)
        # créé sous le umask du script (0700), pas restreint après coup
        self.assertEqual(self.mode_vu_par_le_helper(), "absent")

    def test_repertoire_preexistant_0755_restreint(self):
        os.makedirs(self.dumps, mode=0o755)
        os.chmod(self.dumps, 0o755)
        fait = self.run_script()
        self.assertEqual(fait.returncode, 0, fait.stderr)
        self.assertEqual(self.mode(self.dumps), 0o700)
        self.assertEqual(self.mode(self.dump_du_jour()), 0o600)

    def test_dump_du_jour_preexistant_0644_prive_avant_ecriture(self):
        os.makedirs(self.dumps)
        chemin = self.dump_du_jour()
        with open(chemin, "w", encoding="utf-8") as fh:
            fh.write("ancien dump du jour\n")
        os.chmod(chemin, 0o644)
        fait = self.run_script()
        self.assertEqual(fait.returncode, 0, fait.stderr)
        # le helper a bien vu 0600 à l'ouverture : le mode a été corrigé avant,
        # sinon pg_dump aurait réécrit le fichier en 0644.
        self.assertEqual(self.mode_vu_par_le_helper(), "600")
        self.assertEqual(self.mode(chemin), 0o600)
        with open(chemin, encoding="utf-8") as fh:
            self.assertNotIn("ancien", fh.read())

    def test_dumps_anciens_normalises_sans_reecriture(self):
        os.makedirs(self.dumps)
        chemin = self.dump_ancien("ameesh-vieux-2020-01-01.dump", "intact\n")
        fait = self.run_script()
        self.assertEqual(fait.returncode, 0, fait.stderr)
        self.assertEqual(self.mode(chemin), 0o600)
        with open(chemin, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "intact\n")

    def test_dump_partage_non_normalise(self):
        """Un dump à plusieurs liens est refusé comme partagé par le
        catalogue : le script ne le réécrit pas, car changer son mode
        changerait aussi celui des autres chemins du même inode."""
        os.makedirs(self.dumps)
        chemin = self.dump_ancien("ameesh-vieux-2020-01-02.dump", "partage\n")
        autre = os.path.join(self.tmp, "autre-chemin.dump")
        os.link(chemin, autre)
        fait = self.run_script()
        self.assertEqual(fait.returncode, 0, fait.stderr)
        self.assertEqual(self.mode(chemin), 0o644)
        self.assertEqual(self.mode(autre), 0o644)
        with open(chemin, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "partage\n")

    def test_les_anciens_dumps_passent_par_la_retention_privee(self):
        """Le script ne supprime jamais un dump lui-même : un ensemble inscrit
        au catalogue survit, et le seul chemin d'expiration est le catalogue
        privé (`expire-unregistered` puis `prune`, #364/#1484)."""
        os.makedirs(self.dumps)
        ancien = self.dump_ancien("ameesh-vieux-2019-12-31.dump", "vieux\n",
                                  age_jours=20)
        inscrit = self.dump_ancien("ameesh-%s-2019-12-30.dump" % HOTE,
                                   "inscrit\n", age_jours=20)
        fait = self.run_script()
        self.assertEqual(fait.returncode, 0, fait.stderr)
        # aucun `find -delete` brut : les deux fichiers sont encore là, privés,
        # et l'inscrit n'a pas été réécrit.
        self.assertTrue(os.path.exists(ancien), "un dump a été supprimé par le script")
        self.assertTrue(os.path.exists(inscrit), "un ensemble inscrit a été supprimé")
        self.assertEqual(self.mode(ancien), 0o600)
        self.assertEqual(self.mode(inscrit), 0o600)
        with open(inscrit, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "inscrit\n")
        appels = self.retention_argv()
        self.assertEqual(len(appels), 2, appels)
        expire, prune = appels
        self.assertIn("expire-unregistered", expire)
        self.assertIn("--max-age", expire)
        self.assertEqual(expire[expire.index("--max-age") + 1], "1209600")
        self.assertIn("prune", prune)
        for appel in appels:
            self.assertIn("--root", appel)
            self.assertEqual(appel[appel.index("--root") + 1], self.dumps)


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
        """Un dump nouveau reste privé malgré une ACL par défaut permissive du
        répertoire (#1309) : le helper réel publie un fichier déjà 0600 (#1484)
        et la normalisation du script, après la rétention, couvre le reste."""
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
        # plus de précréation (#1309) : le helper refuse une sortie existante
        # (#1484) ; le fichier naît sous l'ACL puis est normalisé 0600.
        self.assertEqual(self.mode_vu_par_le_helper(), "absent")
        self.assertEqual(self.mode(self.dump_du_jour()), 0o600)



if __name__ == "__main__":
    unittest.main()
