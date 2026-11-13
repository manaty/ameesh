# SPDX-License-Identifier: AGPL-3.0-only
"""#5976 — chaque session d'ameesh se nomme dans `pg_stat_activity`.

Diagnostic #5972 : ni `application_name`, ni `fallback_application_name`, ni
PGAPPNAME n'étaient posés ; toutes les sessions se ressemblaient. Témoins :
- la forme `ameesh/<famille>/<agent ou rôle>@<hôte>`, ASCII, au plus 63
  octets, l'hôte gardé quand l'agent est raccourci ;
- la famille d'un bloc (`application`) ne vaut que pour ce fil ;
- psycopg reçoit `fallback_application_name`, psql PGAPPNAME (sauf s'il est
  déjà posé) : un `application_name` explicite reste maître ;
- `ScopedDb` nomme la passe ; la session réelle des deux pilotes porte le nom.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import os
import threading
import unittest
from unittest import mock

from ameesh import config as config_mod
from ameesh import db as db_mod
from ameesh.scoped_db import ScopedDb
from tests.support import TEST_DSN, PgTestCase

HAVE_PSYCOPG = importlib.util.find_spec("psycopg") is not None


class _Restore(unittest.TestCase):
    def setUp(self):
        saved = dict(db_mod._APP_DEFAULT)
        self.addCleanup(db_mod._APP_DEFAULT.update, saved)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in ("AMEESH_ROLE", "AGENT_MAIL_NAME", "PGAPPNAME"):
            os.environ.pop(key, None)
        host = mock.patch.object(db_mod.socket, "gethostname",
                                 return_value="ameesh-agents-3.manaty.net")
        host.start()
        self.addCleanup(host.stop)
        db_mod.set_application(None, "")


class ApplicationNameTest(_Restore):
    def test_forme(self):
        db_mod.set_application("runner.main", "mty-amh-impl-13")
        self.assertEqual(db_mod.application_name(),
                         "ameesh/runner.main/mty-amh-impl-13@ameesh-agents-3")

    def test_defauts_et_environnement(self):
        self.assertEqual(db_mod.application_name(), "ameesh/cli/-@ameesh-agents-3")
        os.environ["AGENT_MAIL_NAME"] = "mesh-design"
        self.assertEqual(db_mod.application_name(),
                         "ameesh/cli/mesh-design@ameesh-agents-3")
        os.environ["AMEESH_ROLE"] = "pilote"
        self.assertEqual(db_mod.application_name(), "ameesh/cli/pilote@ameesh-agents-3")

    def test_commande_et_minuterie(self):
        db_mod.set_command_application("work")
        self.assertTrue(db_mod.application_name().startswith("ameesh/cli.work/"))
        os.environ["AMEESH_ROLE"] = "auditeur"
        db_mod.set_command_application("work")
        self.assertEqual(db_mod.application_name(),
                         "ameesh/timer.auditeur/auditeur@ameesh-agents-3")

    def test_caracteres_et_longueur(self):
        db_mod.set_application("runner.scoped.branch_sweep", "a,b é\n" + "x" * 80)
        name = db_mod.application_name()
        self.assertLessEqual(len(name.encode("utf-8")), 63)
        self.assertTrue(name.isascii())
        self.assertTrue(name.startswith("ameesh/runner.scoped.branch_sweep/a_b___"), name)
        self.assertTrue(name.endswith("@ameesh-agents-3"), name)

    def test_bloc_local_au_fil(self):
        db_mod.set_application("runner.main", "r")
        seen = {}
        with db_mod.application("peek", "agent-x"):
            self.assertEqual(db_mod.application_name(),
                             "ameesh/peek/agent-x@ameesh-agents-3")
            other = threading.Thread(
                target=lambda: seen.setdefault("name", db_mod.application_name()))
            other.start()
            other.join()
            with db_mod.application("lease"):
                self.assertEqual(db_mod.application_name(),
                                 "ameesh/lease/agent-x@ameesh-agents-3")
        self.assertEqual(seen["name"], "ameesh/runner.main/r@ameesh-agents-3")
        self.assertEqual(db_mod.application_name(), "ameesh/runner.main/r@ameesh-agents-3")


class DriverWiringTest(_Restore):
    def cfg(self, **kw):
        base = dict(dsn="postgresql://u@h/db", schema="public", statement_timeout_ms=0)
        base.update(kw)
        return config_mod.Config(**base)

    def test_psql_pgappname(self):
        db_mod.set_application("serve")
        with mock.patch.object(db_mod.shutil, "which", return_value="/usr/bin/psql"), \
                mock.patch.object(db_mod, "_psql_compatible_dsn", side_effect=lambda d, b: d), \
                mock.patch.object(db_mod.PsqlDriver, "_check"):
            driver = db_mod.PsqlDriver(self.cfg())
            self.assertEqual(driver.env["PGAPPNAME"], "ameesh/serve/-@ameesh-agents-3")
            os.environ["PGAPPNAME"] = "choisi"
            driver = db_mod.PsqlDriver(self.cfg())
            self.assertEqual(driver.env["PGAPPNAME"], "choisi")

    @unittest.skipUnless(HAVE_PSYCOPG, "psycopg absent (part psql)")
    def test_psycopg_fallback(self):
        db_mod.set_application("runner.main", "a")
        with mock.patch("psycopg.connect") as connect:
            with db_mod.application("runner.scoped.balance"):
                db_mod.PsycopgDriver(self.cfg())
        self.assertEqual(connect.call_args.kwargs["fallback_application_name"],
                         "ameesh/runner.scoped.balance/a@ameesh-agents-3")
        self.assertNotIn("application_name", connect.call_args.kwargs)

    @unittest.skipUnless(HAVE_PSYCOPG, "psycopg absent (part psql)")
    def test_psycopg_reconnexion_garde_le_nom(self):
        # #5986 : la reconnexion (L72) se fait hors du bloc de l'appelant
        db_mod.set_application("runner.main", "r")
        with mock.patch("psycopg.connect") as connect:
            with db_mod.application("test.reconnect", "x"):
                driver = db_mod.PsycopgDriver(self.cfg())
            driver._connect()
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(connect.call_args.kwargs["fallback_application_name"],
                         "ameesh/test.reconnect/x@ameesh-agents-3")

    def test_scoped_db_nomme_la_passe(self):
        db_mod.set_application("runner.main", "a")
        names = []

        class Fake:
            def query(self, sql, params=()):
                return []

            def close(self):
                pass

        def connect(cfg):
            names.append(db_mod.application_name())
            return Fake()

        with mock.patch.object(db_mod, "connect", side_effect=connect):
            ScopedDb(self.cfg(), family="runner.scoped.branch_sweep").query("SELECT 1")
            ScopedDb(self.cfg()).query("SELECT 1")
        self.assertEqual(names, ["ameesh/runner.scoped.branch_sweep/a@ameesh-agents-3",
                                 "ameesh/runner.main/a@ameesh-agents-3"])


class LeaseRenewTest(_Restore):
    """Revue ameesh-cx1 (B) : la session du bail est nommée à son ouverture,
    pas après (les deux pilotes la nomment dans leur constructeur)."""

    def test_renew_call_nomme_la_session(self):
        from types import SimpleNamespace
        from ameesh import runner as runner_mod
        db_mod.set_application("runner.main", "r")
        names = []

        class Fake:
            def close(self):
                pass

        def connect(cfg, writer=True):
            names.append(db_mod.application_name())
            return Fake()

        worker = SimpleNamespace(
            db=SimpleNamespace(cfg=config_mod.Config(dsn="postgresql://u@h/db")),
            name="mty-amh-impl-13", epoch=4, lease_held_until=0.0,
            # #7052 : `_renew_call` lit le TTL effectif du worker, pas celui
            # du runner ; la doublure porte donc les deux.
            lease_ttl=100,
            _held_until=lambda: 0.0, renewals=[], call_budget=lambda: 5.0,
            runner=SimpleNamespace(runner_id="rid", lease_ttl=100))
        with mock.patch.object(runner_mod.db_mod, "connect", side_effect=connect), \
                mock.patch.object(runner_mod.migrations, "require_compatible_writer"), \
                mock.patch.object(runner_mod.registry, "renew", return_value=123.0):
            done, expires, error = runner_mod.AgentWorker._renew_call(worker)
        self.assertEqual((done, expires, error), (True, 123.0, None))
        self.assertEqual(names, ["ameesh/lease/mty-amh-impl-13@ameesh-agents-3"])


class SessionTest(PgTestCase):
    """La session réelle porte le nom ; un application_name du DSN prime."""

    def setUp(self):
        super().setUp()
        saved = dict(db_mod._APP_DEFAULT)
        self.addCleanup(db_mod._APP_DEFAULT.update, saved)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("PGAPPNAME", None)

    def _name(self, conn) -> str:
        return conn.query("SELECT current_setting('application_name') AS n")[0]["n"]

    def _check(self, driver):
        with db_mod.application("test.5976", "temoin"):
            conn = self.connect(driver=driver)
        try:
            self.assertEqual(self._name(conn), self._expected())
        finally:
            conn.close()
        dsn = TEST_DSN + ("&" if "?" in TEST_DSN else "?") + "application_name=explicite"
        conn = db_mod.connect(dataclasses.replace(self.cfg, dsn=dsn, driver=driver,
                                                  schema=self.schema))
        try:
            self.assertEqual(self._name(conn), "explicite")
        finally:
            conn.close()

    def _expected(self) -> str:
        with db_mod.application("test.5976", "temoin"):
            return db_mod.application_name()

    @unittest.skipUnless(HAVE_PSYCOPG, "psycopg absent (part psql)")
    def test_psycopg(self):
        self._check("psycopg")

    def test_psql(self):
        self._check("psql")


if __name__ == "__main__":
    unittest.main()
