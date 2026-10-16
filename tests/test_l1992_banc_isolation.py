# SPDX-License-Identifier: AGPL-3.0-only
"""#1992 : chaque exécution de scripts/test.sh sur le banc partagé prend sa
propre base Postgres, jetable à la sortie.

Le banc local (`scripts/pg-up.sh`) est partagé par tous les agents de l'hôte :
sur la base fixe `agent_mesh`, le LISTEN d'une exécution reçoit les NOTIFY
d'une autre (faux rouge de `tests.test_work.WorkTest.
test_notify_sur_changement_d_etat`, constat impl-08 du 16/10). Ce module ne se
connecte à aucun serveur : `psql` et les interprètes sont des doublures, et
seule la mécanique est vérifiée — une base par exécution, DSN identique
transmis aux deux passes, suppression à la sortie même en échec, DSN explicite
(CI, job déporté) et banc déjà prêt respectés.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from . import support  # noqa: F401  (L142 : isole l'environnement de l'hôte)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "test.sh"
DEFAULT_DSN = "postgresql://agent_mesh@127.0.0.1:55432/agent_mesh"
TIMEOUT_S = 120
CREATE_RE = re.compile(r'^CREATE DATABASE "([^"]+)"$')
DROP_RE = re.compile(r'^DROP DATABASE IF EXISTS "([^"]+)" WITH \(FORCE\)$')

#: doublure `psql` : enregistre chaque appel, ne touche à aucun serveur
PSQL_STUB = """#!{python}
import json, os, sys
with open(os.environ["PSQL_CALLS"], "a") as out:
    out.write(json.dumps(sys.argv[1:]) + "\\n")
"""

#: doublure des interprètes : enregistre le DSN vu par chaque passe
PROXY_STUB = """#!{python}
import json, os, sys
if sys.argv[1:2] == ["-c"]:
    sys.exit(0 if os.path.exists("deps-ready") else 1)
if os.environ.get("FAIL_DRIVER") == os.environ.get("AMEESH_DRIVER"):
    sys.exit(3)
with open(os.environ["DSN_CALLS"], "a") as out:
    out.write(json.dumps({{"driver": os.environ.get("AMEESH_DRIVER"),
                          "dsn": os.environ.get("AMEESH_TEST_DSN")}}) + "\\n")
"""


class BenchIsolationTest(unittest.TestCase):
    """Le banc partagé : une base jetable par exécution de scripts/test.sh."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for directory in ("scripts", "tests", "fakebin", ".venv/bin"):
            (self.root / directory).mkdir(parents=True)
        shutil.copy2(SCRIPT, self.root / "scripts/test.sh")
        pg_up = self.root / "scripts/pg-up.sh"
        pg_up.write_text("#!/usr/bin/env bash\nexit 0\n")
        pg_up.chmod(0o755)
        (self.root / "tests/__init__.py").touch()
        (self.root / "deps-ready").touch()
        pip = self.root / ".venv/bin/pip"
        pip.write_text("#!/usr/bin/env bash\ntouch deps-ready\n")
        pip.chmod(0o755)
        for path in ("fakebin/psql", "fakebin/python3", ".venv/bin/python"):
            stub = PSQL_STUB if path.endswith("psql") else PROXY_STUB
            target = self.root / path
            target.write_text(stub.format(python=sys.executable))
            target.chmod(0o755)
        self.psql_calls = self.root / "psql-calls"
        self.dsn_calls = self.root / "dsn-calls"
        base = {name: value for name, value in os.environ.items()
                if name not in ("AMEESH_TEST_DSN", "AGENT_MESH_TEST_DSN",
                                "AMEESH_PG_UP", "AMEESH_DRIVER", "AGENT_MESH_DRIVER")}
        self.env = dict(base, AMEESH_TEST_VENV=".venv", PYTHONPATH="",
                        PSQL_CALLS=str(self.psql_calls), DSN_CALLS=str(self.dsn_calls))
        self.env["PATH"] = str(self.root / "fakebin") + os.pathsep + os.environ["PATH"]

    # -- doublures ---------------------------------------------------------
    def lancer(self, *args, **extra):
        """Démarre scripts/test.sh sous doublures, sans l'attendre."""
        return subprocess.Popen(
            ["bash", str(self.root / "scripts/test.sh"), *args],
            cwd=self.root, env=dict(self.env, **extra), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def attendre(self, *procs):
        for proc in procs:
            out, _ = proc.communicate(timeout=TIMEOUT_S)
            self.assertEqual(proc.returncode, 0, out)

    def appels_psql(self):
        if not self.psql_calls.exists():
            return []
        return [json.loads(line) for line in self.psql_calls.read_text().splitlines()]

    def dsn_vus(self):
        if not self.dsn_calls.exists():
            return []
        return [json.loads(line) for line in self.dsn_calls.read_text().splitlines()]

    @staticmethod
    def commande(appel):
        """Le `-c` d'un appel psql, ou None."""
        return appel[appel.index("-c") + 1] if "-c" in appel else None

    @staticmethod
    def nom_base(dsn):
        return dsn.rsplit("/", 1)[-1] if dsn else None

    def bases_creees_et_supprimees(self):
        creates, drops = [], []
        for appel in self.appels_psql():
            commande = self.commande(appel)
            if commande and CREATE_RE.match(commande):
                creates.append(CREATE_RE.match(commande).group(1))
            if commande and DROP_RE.match(commande):
                drops.append(DROP_RE.match(commande).group(1))
            self.assertIn(DEFAULT_DSN, appel,
                          "les gestes de base passent par la base de service")
        return creates, drops

    # -- témoins -----------------------------------------------------------
    def test_deux_executions_concurrentes_ont_des_bases_distinctes(self):
        """Deux exécutions simultanées ne partagent plus la base fixe."""
        self.attendre(self.lancer(), self.lancer())
        creates, drops = self.bases_creees_et_supprimees()
        self.assertEqual(len(creates), 2, creates)
        self.assertEqual(len(set(creates)), 2, creates)
        self.assertCountEqual(drops, creates)
        vus = self.dsn_vus()
        self.assertEqual(len(vus), 4, vus)  # deux passes par exécution
        self.assertCountEqual([self.nom_base(v["dsn"]) for v in vus], creates * 2)
        self.assertEqual({v["driver"] for v in vus}, {"psql", "psycopg"})

    def test_dsn_explicite_est_respecte_tel_quel(self):
        """CI, job déporté, outil : leur DSN ne déclenche aucun geste de base."""
        explicite = "postgresql://banc@127.0.0.1:1/banc"
        self.attendre(self.lancer(AMEESH_TEST_DSN=explicite))
        self.assertEqual(self.appels_psql(), [])
        vus = self.dsn_vus()
        self.assertEqual(len(vus), 2, vus)
        self.assertEqual({v["dsn"] for v in vus}, {explicite})

    def test_banc_deja_pret_ne_cree_rien(self):
        """AMEESH_PG_UP=0 (L159b) : la base du job est déjà là, on n'y touche pas."""
        self.attendre(self.lancer(AMEESH_PG_UP="0"))
        self.assertEqual(self.appels_psql(), [])
        vus = self.dsn_vus()
        self.assertEqual(len(vus), 2, vus)
        self.assertEqual({v["dsn"] for v in vus}, {DEFAULT_DSN})

    def test_echec_d_une_passe_supprime_la_base(self):
        """Un rouge n'abandonne pas la base jetable dans le conteneur partagé."""
        proc = self.lancer(FAIL_DRIVER="psql")
        out, _ = proc.communicate(timeout=TIMEOUT_S)
        self.assertNotEqual(proc.returncode, 0, out)
        creates, drops = self.bases_creees_et_supprimees()
        self.assertEqual(len(creates), 1, creates)
        self.assertEqual(drops, creates)


if __name__ == "__main__":
    unittest.main()
