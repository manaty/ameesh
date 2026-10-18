# SPDX-License-Identifier: AGPL-3.0-only
"""#2170 : le profil de connexion du banc transmis à la base jetable.

`scripts/test.sh` crée des bases jetables (`agent_mesh_test_*`, #1992) alors
que le profil `~/.pgpass` du poste ne couvre souvent que `agent_mesh` : libpq
ne trouve alors aucun mot de passe. `scripts/pgpass-banc.py` lit la première
entrée qui couvre la connexion d'administration et écrit un `PGPASSFILE`
privé (0600) pour les bases du banc ; le script de test le passe aux deux
pilotes et le supprime à la sortie. Ce module vérifie le fichier lui-même,
puis son branchement dans `scripts/test.sh` avec des doublures (aucun serveur,
aucun vrai secret).
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from . import support  # noqa: F401  (L142 : isole l'environnement de l'hôte)

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "scripts" / "pgpass-banc.py"
TEST_SH = REPO / "scripts" / "test.sh"
ADMIN_DSN = "postgresql://agent_mesh@127.0.0.1:55432/agent_mesh"
SECRET = "s3cret:du\\banc"
SEGMENT = "s3cret\\:du\\\\banc"

SPEC = importlib.util.spec_from_file_location("pgpass_banc", HELPER)
PGPASS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PGPASS)


class ProfilTest(unittest.TestCase):
    """Le fichier de profil : correspondance, échappements, refus."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ameesh-2170-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.passfile = self.root / "pgpass"
        self.output = self.root / "sortie"
        self.passfile.write_text(
            "127.0.0.1:55432:agent_mesh:agent_mesh:%s\n" % SEGMENT)
        self.passfile.chmod(0o600)

    def lancer(self, *args, source=None, environ=None):
        base = {} if source is False else {"PGPASSFILE": str(source or self.passfile)}
        base.update(environ or {})
        with mock.patch.dict(os.environ, base, clear=False):
            os.environ.pop("PGPASSFILE", None)
            if "PGPASSFILE" in base:
                os.environ["PGPASSFILE"] = base["PGPASSFILE"]
            os.environ.pop("PGPASSWORD", None)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = PGPASS.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def appeler(self, **kwargs):
        return self.lancer(ADMIN_DSN, str(self.output), **kwargs)

    def test_uri_du_banc(self):
        self.assertEqual(PGPASS.parse_dsn(ADMIN_DSN),
                         ("127.0.0.1", "55432", "agent_mesh", "agent_mesh"))

    def test_entree_du_banc_transmise_pour_les_bases_jetables(self):
        code, out, err = self.appeler()
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(self.output.read_text(),
                         "127.0.0.1:55432:*:agent_mesh:%s\n" % SEGMENT)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)

    def test_le_secret_ne_sort_jamais(self):
        code, out, err = self.appeler()
        self.assertEqual(code, 0)
        self.assertNotIn(SECRET, out + err)
        self.assertNotIn(SECRET, self.output.read_text())
        self.assertIn(SEGMENT, self.output.read_text())

    def test_joker_de_base_et_premier_match(self):
        self.passfile.write_text(
            "127.0.0.1:55432:*:agent_mesh:premier\n"
            "127.0.0.1:55432:agent_mesh:agent_mesh:second\n")
        code, _, _ = self.appeler()
        self.assertEqual(code, 0)
        self.assertIn(":premier\n", self.output.read_text())

    def test_aucune_entree(self):
        self.passfile.write_text("127.0.0.1:55432:autre:agent_mesh:secret\n")
        code, out, err = self.appeler()
        self.assertEqual((code, out), (3, ""))
        self.assertIn("aucune entrée", err)
        self.assertFalse(self.output.exists())

    def test_profil_trop_ouvert_refuse(self):
        self.passfile.chmod(0o644)
        code, out, err = self.appeler()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("groupe ou aux autres", err)
        self.assertFalse(self.output.exists())

    def test_profil_absent_refuse(self):
        code, out, err = self.appeler(source=self.root / "absent")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("illisible", err)

    def test_echappement_de_n_reste_sur_une_ligne(self):
        self.passfile.write_text(
            "127.0.0.1:55432:agent_mesh:agent_mesh:deux\\nlignes\n")
        code, out, err = self.lancer(ADMIN_DSN, str(self.output))
        self.assertEqual((code, out, err), (0, "", ""))
        # `\n` est un échappement de libpq : le mot de passe tient sur une
        # ligne (le `n` est le caractère échappé).
        self.assertIn("deuxnlignes", self.output.read_text())

    def test_vrai_saut_de_ligne_refuse(self):
        with self.assertRaises(PGPASS.Refus):
            PGPASS.write_passfile(str(self.output), "127.0.0.1", "55432",
                                  "agent_mesh", "deux\nlignes")
        self.assertFalse(self.output.exists())

    def test_pgpassfile_prime_sur_home(self):
        autre = self.root / "home"
        autre.mkdir()
        (autre / ".pgpass").write_text("127.0.0.1:55432:agent_mesh:agent_mesh:home\n")
        (autre / ".pgpass").chmod(0o600)
        code, _, _ = self.appeler(source=False, environ={"HOME": str(autre)})
        self.assertEqual(code, 0)
        self.assertIn(":*:agent_mesh:home\n", self.output.read_text())


#: doublure `psql` : journalise l'appel et l'environnement du profil ; la
#: garde `SELECT 1` n'accepte que PGPASSWORD ou une entrée qui couvre la base.
PSQL_STUB = r"""#!{python}
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["PSQL_CALLS"], "a") as out:
    out.write(json.dumps({{"argv": argv,
                          "pgpassfile": os.environ.get("PGPASSFILE")}}) + "\n")
if "-c" in argv and argv[argv.index("-c") + 1] == "SELECT 1":
    dsn = argv[argv.index("--dbname") + 1]
    db = dsn.rsplit("/", 1)[-1]
    ok = bool(os.environ.get("PSQL_TRUST") or os.environ.get("PGPASSWORD"))
    pf = os.environ.get("PGPASSFILE")
    if pf:
        try:
            with open(pf, encoding="utf-8") as stream:
                ok = ok or any(line.split(":")[2] in ("*", db)
                               for line in stream.read().splitlines()
                               if line and not line.startswith("#"))
        except (OSError, IndexError):
            pass
    if not ok:
        sys.exit(2)
"""

#: doublure des interprètes : transmet la vraie aide pgpass, journalise le
#: profil vu par chaque passe.
PYTHON_STUB = r"""#!{python}
import json, os, sys
if len(sys.argv) > 1 and sys.argv[1].endswith("pgpass-banc.py"):
    os.execv(sys.executable, [sys.executable] + sys.argv[1:])
if sys.argv[1:2] == ["-c"]:
    sys.exit(0)
profile = os.environ.get("PGPASSFILE")
content = None
if profile:
    try:
        content = open(profile, encoding="utf-8").read()
    except OSError:
        content = None
with open(os.environ["DSN_CALLS"], "a") as out:
    out.write(json.dumps({{"driver": os.environ.get("AMEESH_DRIVER"),
                          "dsn": os.environ.get("AMEESH_TEST_DSN"),
                          "pgpassfile": profile,
                          "pgpass": content}}) + "\n")
"""


class BranchementTest(unittest.TestCase):
    """`scripts/test.sh` : le profil privé va aux deux passes, puis disparaît."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ameesh-2170-sh-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "travail"
        for directory in ("scripts", "tests", "fakebin", ".venv/bin"):
            (self.work / directory).mkdir(parents=True)
        shutil.copy2(TEST_SH, self.work / "scripts/test.sh")
        shutil.copy2(HELPER, self.work / "scripts/pgpass-banc.py")
        (self.work / "scripts/pg-up.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
        (self.work / "scripts/pg-up.sh").chmod(0o755)
        (self.work / "tests/__init__.py").touch()
        for path in ("fakebin/psql", "fakebin/python3", ".venv/bin/python"):
            stub = PSQL_STUB if path.endswith("psql") else PYTHON_STUB
            target = self.work / path
            target.write_text(stub.format(python=sys.executable))
            target.chmod(0o755)
        self.psql_calls = self.root / "psql-calls"
        self.dsn_calls = self.root / "dsn-calls"
        self.passfile = self.root / "pgpass"
        self.passfile.write_text(
            "127.0.0.1:55432:agent_mesh:agent_mesh:%s\n" % SEGMENT)
        self.passfile.chmod(0o600)
        self.tmpdir = self.root / "tmp"
        self.tmpdir.mkdir()
        base = {name: value for name, value in os.environ.items()
                if name not in ("AMEESH_TEST_DSN", "AGENT_MESH_TEST_DSN",
                                "AMEESH_PG_UP", "AMEESH_DRIVER", "AGENT_MESH_DRIVER",
                                "PGPASSWORD", "PGPASSFILE", "TMPDIR")}
        self.env = dict(base, AMEESH_TEST_VENV=".venv", PYTHONPATH="",
                        TMPDIR=str(self.tmpdir),
                        PSQL_CALLS=str(self.psql_calls),
                        DSN_CALLS=str(self.dsn_calls))
        self.env["PATH"] = str(self.work / "fakebin") + os.pathsep + os.environ["PATH"]

    def lancer(self, **extra):
        env = dict(self.env, **extra)
        for name, value in list(env.items()):
            if value is None:
                env.pop(name)
        return subprocess.run(
            ["bash", str(self.work / "scripts/test.sh")],
            cwd=self.work, env=env, text=True, capture_output=True, timeout=120)

    def appels(self, path):
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_profil_du_banc_couvre_les_deux_passes(self):
        proc = self.lancer(PGPASSFILE=str(self.passfile))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn(SECRET, proc.stdout + proc.stderr)
        vus = self.appels(self.dsn_calls)
        self.assertEqual(len(vus), 2, vus)
        self.assertEqual({v["driver"] for v in vus}, {"psql", "psycopg"})
        profils = {v["pgpassfile"] for v in vus}
        self.assertEqual(len(profils), 1, profils)
        profil = Path(profils.pop())
        self.assertEqual(profil.parent.parent, self.tmpdir)
        self.assertTrue(profil.name.endswith("pgpass"))
        self.assertIn(":*:agent_mesh:", vus[0]["pgpass"])
        self.assertIn(SEGMENT, vus[0]["pgpass"])
        self.assertFalse(profil.exists(), "le profil privé survit à la sortie")
        self.assertEqual(self.passfile.read_text(),
                         "127.0.0.1:55432:agent_mesh:agent_mesh:%s\n" % SEGMENT)
        # les gestes d'administration gardent le profil du poste ; seule la
        # garde de la base jetable reçoit le profil dérivé
        psql = self.appels(self.psql_calls)
        crees = [a for a in psql if any("CREATE DATABASE" in str(x) for x in a["argv"])]
        gardes = [a for a in psql if "SELECT 1" in a["argv"]]
        drops = [a for a in psql if any("DROP DATABASE" in str(x) for x in a["argv"])]
        self.assertEqual((len(crees), len(gardes), len(drops)), (1, 1, 1), psql)
        self.assertEqual(crees[0]["pgpassfile"], str(self.passfile))
        self.assertEqual(drops[0]["pgpassfile"], str(self.passfile))
        for garde in gardes:
            self.assertEqual(Path(garde["pgpassfile"]), profil)
        self.assertEqual(list(self.tmpdir.glob("ameesh-pgpass-*")), [])

    def test_pgpassword_evite_la_derivation(self):
        proc = self.lancer(PGPASSWORD="secret-du-banc", PGPASSFILE=None)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        vus = self.appels(self.dsn_calls)
        self.assertEqual(len(vus), 2, vus)
        self.assertTrue(all(v["pgpassfile"] is None for v in vus), vus)
        self.assertEqual(list(self.tmpdir.glob("ameesh-pgpass-*")), [])

    def test_sans_entree_la_garde_dit_le_contrat(self):
        self.passfile.write_text("127.0.0.1:55432:autre:agent_mesh:secret\n")
        proc = self.lancer(PGPASSFILE=str(self.passfile))
        self.assertNotEqual(proc.returncode, 0)
        # le refus du helper reste utile quand la garde échoue
        self.assertIn("aucune entrée", proc.stderr)
        self.assertIn("base jetable", proc.stderr)
        self.assertIn("PGPASSWORD", proc.stderr)
        self.assertIn("~/.pgpass", proc.stderr)
        self.assertEqual(self.appels(self.dsn_calls), [])
        self.assertEqual(list(self.tmpdir.glob("ameesh-pgpass-*")), [])

    def test_sans_entree_mais_serveur_sans_mot_de_passe_sans_bruit(self):
        # le profil ne couvre pas le banc, mais le serveur accepte la
        # connexion sans mot de passe : le refus du helper ne s'affiche pas
        self.passfile.write_text("127.0.0.1:55432:autre:agent_mesh:secret\n")
        proc = self.lancer(PGPASSFILE=str(self.passfile), PSQL_TRUST="1")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("aucune entrée", proc.stderr)
        self.assertNotIn("pgpass-banc", proc.stderr)
        vus = self.appels(self.dsn_calls)
        self.assertEqual(len(vus), 2, vus)
        # aucun profil dérivé : les passes gardent le profil du poste
        self.assertTrue(all(v["pgpassfile"] == str(self.passfile) for v in vus), vus)
        self.assertEqual(list(self.tmpdir.glob("ameesh-pgpass-*")), [])


if __name__ == "__main__":
    unittest.main()
