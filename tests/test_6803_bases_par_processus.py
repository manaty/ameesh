# SPDX-License-Identifier: AGPL-3.0-only
"""#6803 : une base jetable par processus d'une part (`test-parts.py run
--parallel`).

Un schéma ne cloisonne pas les verrous consultatifs : leur clé vaut pour toute
la base. `vm_adapter.tick_host` prend
`pg_try_advisory_xact_lock(hashtextextended('vm-adapter:' || hôte, 0))` ; dix
classes de test jouent l'hôte `burst`, et deux processus de la même part se
refusaient le verrou (test_6074 « worker busy », run 42932684029). Le témoin
joue deux processus sur le serveur du banc : la clé constante tenue dans la
base de l'un reste libre dans celle de l'autre, alors qu'elle est refusée dans
la même base.

#6808 : le DSN du banc peut prendre d'autres formes URL que `//hôte/base` —
`postgresql:///base?host=/var/run/postgresql` (hôte vide) ou un `dbname=` de
la requête. La base jetable doit rester valide et la sonde vérifier la base
réellement atteinte (`current_database()`), sinon les processus restent
cloisonnés nulle part, ou sur une autre base, sans le dire.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

from tests import support

REPO = pathlib.Path(__file__).resolve().parent.parent


def _charger():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "parts_6803", REPO / "scripts" / "test-parts.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["parts_6803"] = module
    spec.loader.exec_module(module)
    return module


parts = _charger()

#: doublure `psql` : enregistre chaque appel, refuse celui qui contient
#: `PSQL_REFUSE` ; à la sonde de base, rend la base du DSN (ou `PSQL_CURRENT_DB`)
PSQL_STUB = """#!{python}
import json, os, sys, urllib.parse
with open(os.environ["PSQL_CALLS"], "a") as out:
    out.write(json.dumps(sys.argv[1:]) + "\\n")
refus = os.environ.get("PSQL_REFUSE")
if refus and refus in " ".join(sys.argv[1:]):
    sys.exit(1)
if "current_database" in " ".join(sys.argv[1:]):
    dsn = sys.argv[sys.argv.index("--dbname") + 1]
    print(os.environ.get("PSQL_CURRENT_DB")
          or urllib.parse.urlsplit(dsn).path.lstrip("/"))
"""

CLE = "hashtextextended('vm-adapter:burst', 0)"


class BasesDoubleesTest(unittest.TestCase):
    """La création, le repli et la suppression, sans serveur."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        racine = pathlib.Path(tmp.name)
        psql = racine / "psql"
        psql.write_text(PSQL_STUB.format(python=sys.executable))
        psql.chmod(0o755)
        self.appels = racine / "appels.jsonl"
        self.env = {"PATH": f"{racine}:{os.environ.get('PATH', '')}",
                    "PSQL_CALLS": str(self.appels),
                    "AMEESH_TEST_DSN": "postgresql://ameesh@127.0.0.1:5432/ameesh"}

    def _sql(self):
        if not self.appels.exists():
            return []
        return [json.loads(ligne)[-1] for ligne in self.appels.read_text().splitlines()]

    def test_une_base_distincte_par_processus(self):
        with mock.patch.dict(os.environ, self.env):
            admin, dsns = parts.bases_par_processus(3, 2)
            self.assertEqual(admin, self.env["AMEESH_TEST_DSN"])
            self.assertEqual(len(set(dsns)), 2, dsns)
            bases = [urllib.parse.urlsplit(d).path.lstrip("/") for d in dsns]
            self.assertTrue(all(b.startswith("ameesh_p3_") for b in bases), bases)
            self.assertTrue(all(d.startswith("postgresql://ameesh@127.0.0.1:5432/")
                                for d in dsns), dsns)
            self.assertEqual(self._sql(), [s for b in bases
                                           for s in (f'CREATE DATABASE "{b}"',
                                                     "SELECT current_database()")])
            parts.supprimer_bases(admin, dsns)
        self.assertEqual(self._sql()[4:],
                         [f'DROP DATABASE IF EXISTS "{b}" WITH (FORCE)' for b in bases])

    def test_une_creation_refusee_garde_la_base_partagee(self):
        with mock.patch.dict(os.environ, dict(self.env, PSQL_REFUSE="_2_")):
            _admin, dsns = parts.bases_par_processus(1, 2)
        self.assertEqual(dsns, [])
        sql = self._sql()
        self.assertEqual(len(sql), 4, sql)
        self.assertTrue(sql[0].startswith("CREATE DATABASE"), sql)
        premiere = sql[0].split('"')[1]
        self.assertEqual(sql[3], f'DROP DATABASE IF EXISTS "{premiere}" WITH (FORCE)')

    def test_une_base_injoignable_garde_la_base_partagee(self):
        """B de mty-amh-impl-11 : CREATE DATABASE réussit par le DSN
        d'administration, mais le profil (.pgpass limité à cette base,
        #2170) refuse la base fille ; aucun processus n'y est envoyé."""
        with mock.patch.dict(os.environ, dict(self.env, PSQL_REFUSE="current_database")):
            _admin, dsns = parts.bases_par_processus(1, 2)
        self.assertEqual(dsns, [])
        sql = self._sql()
        premiere = sql[0].split('"')[1]
        self.assertEqual(sql, [f'CREATE DATABASE "{premiere}"', "SELECT current_database()",
                               f'DROP DATABASE IF EXISTS "{premiere}" WITH (FORCE)'])
        appels = [json.loads(l) for l in self.appels.read_text().splitlines()]
        self.assertIn(f"/{premiere}", appels[1][appels[1].index("--dbname") + 1])

    def test_une_url_sans_hote_garde_ses_trois_barres(self):
        """#6808 : `postgresql:///base?host=/var/run/postgresql` ne doit pas
        devenir `postgresql:/base?…`, que libpq refuse."""
        self.assertEqual(
            parts._base_dsn("postgresql:///ameesh?host=/var/run/postgresql", "jetable"),
            "postgresql:///jetable?host=/var/run/postgresql")

    def test_un_dbname_de_la_requete_cede_la_place(self):
        """#6808 : `dbname=` l'emporte sur le chemin chez libpq ; le garder
        enverrait tous les processus sur cette autre base."""
        self.assertEqual(
            parts._base_dsn(
                "postgresql://ameesh@127.0.0.1:5432/ameesh?dbname=autre&sslmode=disable",
                "jetable"),
            "postgresql://ameesh@127.0.0.1:5432/jetable?sslmode=disable")

    def test_une_url_sans_hote_cree_et_sonde_sa_base(self):
        env = dict(self.env,
                   AMEESH_TEST_DSN="postgresql:///ameesh?host=/var/run/postgresql")
        with mock.patch.dict(os.environ, env):
            admin, dsns = parts.bases_par_processus(2, 1)
        self.assertEqual(admin, env["AMEESH_TEST_DSN"])
        self.assertEqual(len(dsns), 1, dsns)
        self.assertTrue(dsns[0].startswith("postgresql:///ameesh_p2_1_"), dsns)
        self.assertTrue(dsns[0].endswith("?host=/var/run/postgresql"), dsns)
        self.assertEqual(self._sql()[-1], "SELECT current_database()")

    def test_une_base_qui_atteint_une_autre_base_replie(self):
        """#6808 : la sonde vérifie la base atteinte ; un DSN qui atterrit
        ailleurs (profil, `dbname=`) rend la base partagée, en le disant."""
        erreurs = io.StringIO()
        with mock.patch.dict(os.environ, dict(self.env, PSQL_CURRENT_DB="autre")):
            with contextlib.redirect_stderr(erreurs):
                _admin, dsns = parts.bases_par_processus(1, 1)
        self.assertEqual(dsns, [])
        self.assertIn("la base atteinte 'autre' n'est pas", erreurs.getvalue())
        self.assertIn("base partagée", erreurs.getvalue())
        sql = self._sql()
        premiere = sql[0].split('"')[1]
        self.assertEqual(sql, [f'CREATE DATABASE "{premiere}"', "SELECT current_database()",
                               f'DROP DATABASE IF EXISTS "{premiere}" WITH (FORCE)'])

    def test_le_diagnostic_ne_divulgue_pas_le_mot_de_passe(self):
        """B de ameesh-cx1 : le repli est le chemin d'une base inattendue ;
        son diagnostic ne doit pas recopier le DSN (userinfo ou `password=`)."""
        secret = "s3cr3t-6808"
        for dsn in (
                f"postgresql://ameesh:{secret}@127.0.0.1:5432/ameesh",
                f"postgresql:///ameesh?user=ameesh&password={secret}&host=127.0.0.1"):
            with self.subTest(dsn=dsn):
                erreurs = io.StringIO()
                env = dict(self.env, AMEESH_TEST_DSN=dsn, PSQL_CURRENT_DB="autre")
                with mock.patch.dict(os.environ, env):
                    with contextlib.redirect_stderr(erreurs):
                        _admin, dsns = parts.bases_par_processus(1, 1)
                self.assertEqual(dsns, [])
                self.assertIn("la base atteinte 'autre' n'est pas", erreurs.getvalue())
                self.assertNotIn(secret, erreurs.getvalue())

    def test_sans_dsn_explicite_rien_n_est_cree(self):
        env = dict(self.env)
        del env["AMEESH_TEST_DSN"]
        with mock.patch.dict(os.environ, env):
            os.environ.pop("AMEESH_TEST_DSN", None)
            os.environ.pop("AGENT_MESH_TEST_DSN", None)
            self.assertEqual(parts.bases_par_processus(1, 2)[1], [])
        self.assertEqual(self._sql(), [])


def _sans_hote(dsn: str) -> str | None:
    """#6808 : la même cible en URL sans hôte, `dbname=` dans la requête.

    `postgresql:///` garde un hôte vide ; les paramètres de la forme à hôte
    passent en clé/valeur, ce que libpq accepte dans la requête."""
    morceaux = urllib.parse.urlsplit(dsn)
    if morceaux.scheme not in ("postgres", "postgresql") or not morceaux.hostname:
        return None
    params = {"host": morceaux.hostname,
              "dbname": morceaux.path.lstrip("/") or "postgres"}
    if morceaux.port:
        params["port"] = str(morceaux.port)
    if morceaux.username:
        params["user"] = morceaux.username
    if morceaux.password:
        params["password"] = morceaux.password
    return "postgresql:///?" + urllib.parse.urlencode(params)


class DeuxProcessusTest(unittest.TestCase):
    """Le témoin réel : deux bases d'une part sur le serveur du banc."""

    def _admin_dsn(self) -> str:
        return support.TEST_DSN

    def setUp(self):
        if shutil.which("psql") is None:
            self.skipTest("psql absent")
        with mock.patch.dict(os.environ, {"AMEESH_TEST_DSN": self._admin_dsn()}):
            self.admin, self.dsns = parts.bases_par_processus(99, 2)
        if not self.dsns:
            self.skipTest("bases jetables refusées ou injoignables sur le banc")
        self.addCleanup(parts.supprimer_bases, self.admin, self.dsns)

    def _try(self, dsn: str) -> str:
        proc = subprocess.run(
            ["psql", "-w", "--dbname", dsn, "-Atc", f"SELECT pg_try_advisory_lock({CLE})"],
            capture_output=True, text=True, timeout=60)
        return proc.stdout.strip()

    def test_la_cle_constante_ne_traverse_pas_les_processus(self):
        tenant = subprocess.Popen(
            ["psql", "-w", "--dbname", self.dsns[0], "-At", "-v", "ON_ERROR_STOP=1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(tenant.wait, 30)
        self.addCleanup(tenant.stdout.close)
        self.addCleanup(tenant.stdin.close)
        tenant.stdin.write(f"SELECT pg_advisory_lock({CLE});\n\\echo tenu\n")
        tenant.stdin.flush()
        lignes = []
        while "tenu" not in lignes:
            ligne = tenant.stdout.readline()
            self.assertTrue(ligne, "le verrou n'a pas été pris : %r" % lignes)
            lignes.append(ligne.strip())
        # témoin négatif : dans la même base, la clé est refusée (le défaut)
        self.assertEqual(self._try(self.dsns[0]), "f")
        # chaque processus a sa base : la clé y reste libre
        self.assertEqual(self._try(self.dsns[1]), "t")


class DeuxProcessusSansHoteTest(DeuxProcessusTest):
    """#6808 : le même cloisonnement par une URL sans hôte et `dbname=`."""

    def _admin_dsn(self) -> str:
        dsn = _sans_hote(support.TEST_DSN)
        if dsn is None:
            self.skipTest("DSN du banc hors forme URL avec hôte")
        return dsn

    def test_les_bases_rendues_sont_valides_et_sans_dbname(self):
        self.assertTrue(all(d.startswith("postgresql:///") for d in self.dsns), self.dsns)
        self.assertTrue(all("dbname=" not in d for d in self.dsns), self.dsns)


if __name__ == "__main__":
    unittest.main()
