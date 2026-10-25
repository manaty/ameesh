# SPDX-License-Identifier: AGPL-3.0-only
"""#3503 — le JIT de Postgres rend `_unreachable_lots` 200 fois plus lente.

Constat du 25/10 : la requête `_unreachable_lots`
(`review_designation_activation.py`) a un coût estimé de 1,26 M, au-dessus de
`jit_above_cost` (100 000) : compilée en JIT, elle prend 42 s, contre 0,2 s
sans, sur 22 lots et 51 agents. Toutes les passes de réassignation des
relecteurs expiraient depuis 03:22Z.

Témoins :
- le réglage part dans le paquet de démarrage (`-c jit=off`) des DEUX pilotes,
  après les options de l'utilisateur (un `-c jit=on` ne peut pas le rallumer) ;
- le chemin où le DSN fixe `options=` (PGOPTIONS ignoré) pose le même `SET`,
  y compris dans la session persistante de `transaction()` ;
- `ameesh doctor` lit la session et le réglage par défaut du rôle et de la
  base, et nomme les deux gestes `ALTER ROLE`/`ALTER DATABASE` ;
- les scripts de déploiement et de mise à jour coupent le JIT pour un NOUVEAU
  rôle ou une NOUVELLE base.
"""
from __future__ import annotations

import dataclasses
import os
import unittest
from unittest import mock
from urllib.parse import quote

from ameesh import config as config_mod
from ameesh import db as db_mod
from ameesh import hostcheck
from tests.support import REPO, TEST_DSN, PgTestCase


class StartupOptionsTest(unittest.TestCase):
    """Le réglage part avec la connexion : zéro aller-retour de plus (L61)."""

    def cfg(self, **kw):
        base = dict(dsn="postgresql://u@h/db", schema="public",
                    statement_timeout_ms=0)
        base.update(kw)
        return config_mod.Config(**base)

    def test_jit_off_par_defaut(self):
        self.assertEqual(db_mod.startup_options(self.cfg()), "-c jit=off")

    def test_jit_off_apres_les_options_de_l_utilisateur(self):
        options = db_mod.startup_options(self.cfg(), "-c jit=on -c work_mem=8MB")
        self.assertTrue(options.startswith("-c jit=on -c work_mem=8MB"), options)
        self.assertTrue(options.endswith("-c jit=off"), options)

    def test_jit_off_avec_les_autres_reglages(self):
        options = db_mod.startup_options(self.cfg(schema="bench", read_only=True))
        self.assertIn('-c search_path="bench"', options)
        self.assertIn("-c default_transaction_read_only=on", options)
        self.assertTrue(options.endswith("-c jit=off"), options)


class _FakeJitDb:
    """Base factice : la session d'abord, puis `pg_db_role_setting`."""

    name = "fake"

    def __init__(self, setting, source="client", rows=(), role="ameesh",
                 database="ameesh"):
        self.setting = setting
        self.source = source
        self.rows = list(rows)
        self.role = role
        self.database = database

    def query(self, sql, params=()):
        if "pg_settings" in sql:
            return [{"role": self.role, "database": self.database,
                     "setting": self.setting, "source": self.source}]
        return list(self.rows)


class DoctorLinesTest(unittest.TestCase):
    """`hostcheck.jit_lines` dit la session ET ce qu'une session hors ameesh
    paierait (rôle/base par défaut)."""

    def test_session_off_et_defaut_off(self):
        lines = hostcheck.jit_lines(_FakeJitDb(
            "off", rows=[{"database": None, "role": "ameesh",
                          "setconfig": ["jit=off"]}]))
        self.assertEqual(len(lines), 1)
        self.assertIn("jit        : off", lines[0])
        self.assertIn("role", lines[0])

    def test_base_off_couvre_tous_les_roles(self):
        lines = hostcheck.jit_lines(_FakeJitDb(
            "off", rows=[{"database": "ameesh", "role": None,
                          "setconfig": ["search_path=public", "jit=off"]}]))
        self.assertEqual(len(lines), 1)
        self.assertIn("jit        : off", lines[0])
        self.assertIn("database", lines[0])

    def test_defaut_on_est_signale_avec_les_deux_gestes(self):
        lines = hostcheck.jit_lines(_FakeJitDb("off"))
        self.assertEqual(len(lines), 1)
        self.assertIn("warning", lines[0])
        self.assertIn("PostgreSQL default", lines[0])
        self.assertIn('ALTER ROLE "ameesh" SET jit = off', lines[0])
        self.assertIn('ALTER DATABASE "ameesh" SET jit = off', lines[0])

    def test_role_off_ne_couvre_que_ce_role(self):
        # Une autre ligne (autre rôle) n'est pas lue par la requête filtrée ;
        # ici on vérifie qu'un rôle explicite est reconnu.
        lines = hostcheck.jit_lines(_FakeJitDb(
            "off", rows=[{"database": None, "role": "ameesh",
                          "setconfig": ["jit=off"]}]))
        self.assertIn("default off", lines[0])

    def test_session_on_est_un_defaut(self):
        lines = hostcheck.jit_lines(_FakeJitDb("on", source="default"))
        self.assertEqual(len(lines), 1)
        self.assertIn("ON in this session", lines[0])
        self.assertIn("db.connect", lines[0])
        self.assertIn("-c jit=off", lines[0])


class SessionTest(PgTestCase):
    """La session réelle des deux pilotes ne compile plus de JIT."""

    def _jit(self, conn) -> str:
        rows = conn.query("SELECT setting FROM pg_settings WHERE name = 'jit'")
        self.assertEqual(len(rows), 1)
        return rows[0]["setting"]

    def test_psycopg(self):
        conn = self.connect(driver="psycopg")
        try:
            self.assertEqual(self._jit(conn), "off")
        finally:
            conn.close()

    def test_psql(self):
        conn = self.connect(driver="psql")
        try:
            self.assertEqual(self._jit(conn), "off")
        finally:
            conn.close()

    def test_un_dsn_qui_fixe_options_est_couvert_psql(self):
        """Le DSN porte `options=`, PGOPTIONS serait ignoré : le pilote psql
        doit poser `SET jit = off` dans son amorce."""
        dsn = TEST_DSN + ("&" if "?" in TEST_DSN else "?") + "options=" + quote(
            "-c statement_timeout=21000")
        cfg = dataclasses.replace(self.cfg, dsn=dsn, driver="psql",
                                  schema=self.schema, statement_timeout_ms=0)
        conn = db_mod.connect(cfg)
        try:
            self.assertEqual(self._jit(conn), "off")
            # le DSN a bien été lu : c'est lui qui pose le délai (le cfg est à 0)
            timeout = conn.query("SELECT setting FROM pg_settings"
                                 " WHERE name = 'statement_timeout'")[0]["setting"]
            self.assertEqual(timeout, "21000")
        finally:
            conn.close()

    def test_un_dsn_qui_fixe_options_est_couvert_psycopg(self):
        dsn = TEST_DSN + ("&" if "?" in TEST_DSN else "?") + "options=" + quote(
            "-c statement_timeout=21000")
        cfg = dataclasses.replace(self.cfg, dsn=dsn, driver="psycopg",
                                  schema=self.schema)
        conn = db_mod.connect(cfg)
        try:
            self.assertEqual(self._jit(conn), "off")
        finally:
            conn.close()

    def test_la_session_persistante_de_transaction_psql_coupe_jit(self):
        """Le chemin `options=` du DSN passe par `_bootstrap` : la session
        persistante (transaction, LISTEN/NOTIFY) doit l'avoir reçu."""
        dsn = TEST_DSN + ("&" if "?" in TEST_DSN else "?") + "options=" + quote(
            "-c statement_timeout=21000")
        cfg = dataclasses.replace(self.cfg, dsn=dsn, driver="psql",
                                  schema=self.schema)
        conn = db_mod.connect(cfg)
        try:
            with conn.transaction() as tx:
                self.assertEqual(
                    tx.query("SELECT current_setting('jit') AS jit")[0]["jit"],
                    "off")
        finally:
            conn.close()

    def test_un_pgoptions_jit_on_ne_rallume_pas_psycopg(self):
        cfg = dataclasses.replace(self.cfg, driver="psycopg")
        with mock.patch.dict(os.environ, {"PGOPTIONS": "-c jit=on"}):
            conn = db_mod.connect(cfg)
        try:
            self.assertEqual(self._jit(conn), "off")
        finally:
            conn.close()

    def test_un_pgoptions_jit_on_ne_rallume_pas_psql(self):
        cfg = dataclasses.replace(self.cfg, driver="psql")
        with mock.patch.dict(os.environ, {"PGOPTIONS": "-c jit=on"}):
            conn = db_mod.connect(cfg)
        try:
            self.assertEqual(self._jit(conn), "off")
        finally:
            conn.close()

    def test_le_doctor_lit_la_session_et_le_defaut(self):
        lines = hostcheck.jit_lines(self.db)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("jit", lines[0])
        self.assertNotIn("ON in this session", lines[0])

    def _doctor(self, lines):
        """Doctor complet, sans ce que le poste ajoute (profils, venv, pool)."""
        from contextlib import redirect_stdout
        import io

        from ameesh import cli
        from ameesh import db_capacity
        from ameesh import venv_guard
        out = io.StringIO()
        with mock.patch.object(hostcheck, "jit_lines", return_value=lines), \
                mock.patch.object(cli, "_profile_audit", return_value=None), \
                mock.patch.object(venv_guard, "installation_problems",
                                  return_value=[]), \
                mock.patch.object(db_capacity, "read", return_value=None), \
                redirect_stdout(out):
            code = cli.cmd_doctor(self.cfg, notify_test=False)
        return code, out.getvalue()

    def test_le_doctor_imprime_la_ligne(self):
        _, text = self._doctor(["jit        : sentinel #3503"])
        self.assertIn("jit        : sentinel #3503", text)

    def test_une_session_jit_on_fait_echouer_le_doctor(self):
        code, text = self._doctor([hostcheck.JIT_SESSION_ON % "default"])
        self.assertIn("ON in this session", text)
        self.assertEqual(code, 1)

    def test_un_defaut_jit_on_ne_change_pas_le_verdict(self):
        code, text = self._doctor(["warning    : sentinel default on (#3503)"])
        self.assertIn("sentinel default on", text)
        self.assertEqual(code, 0)


class DeploiementTest(unittest.TestCase):
    """Un nouveau rôle ou une nouvelle base coupe le JIT, pas seulement la
    session du client (#3503)."""

    def text(self, path: str) -> str:
        with open(os.path.join(REPO, path), encoding="utf-8") as fh:
            return fh.read()

    def test_installation_d_un_serveur(self):
        text = self.text("deploy/serveur/installer-serveur.sh")
        self.assertIn("ALTER ROLE ameesh SET jit = off;", text)
        self.assertIn("ALTER DATABASE ameesh SET jit = off", text)

    def test_nouvelle_base_du_mesh(self):
        self.assertIn("ALTER DATABASE ameesh SET jit = off",
                      self.text("deploy/serveur/basculer-mesh.sh"))

    def test_nouveau_role_de_l_auditeur(self):
        text = self.text("deploy/sql/role-auditeur.sql")
        self.assertIn("ALTER ROLE %I SET jit = off", text)
        self.assertIn("'jit=off'", text)

    def test_mise_a_jour(self):
        text = self.text("deploy/mise-a-jour/poste.sh")
        self.assertIn("ALTER ROLE ameesh SET jit = off", text)
        self.assertIn("ALTER DATABASE ameesh SET jit = off", text)
        self.assertIn("reapply_jit_off", text)


if __name__ == "__main__":
    unittest.main()
