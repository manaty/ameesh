# SPDX-License-Identifier: AGPL-3.0-only
"""#5652 — le repli psql sert avec un DSN partagé qui porte `sslnegotiation`.

Constat (canon sync sur AG1) : le DSN de l'hôte porte
`sslmode=require&sslnegotiation=direct`, écrit pour le psycopg (libpq ≥ 17)
installé dans le venv. Sous le python du système, sans psycopg, le pilote
psql reçoit la même chaîne et le `psql` 16 de l'hôte la refuse en bloc
(`invalid URI query parameter: "sslnegotiation"`) : le repli ne servait
jamais. Le pilote retire maintenant les options que son libpq ne connaît
pas (libpq < 17 pour `sslnegotiation`), garde la chaîne telle quelle quand
le client les connaît ou quand sa version est illisible, et ne sonde pas
`psql --version` quand le DSN ne porte aucune option concernée.

Témoins : `dsn.without_parameter` sur les deux formes que lit libpq (URI,
paires clé/valeur) avec ses négatifs (paramètre absent, chaîne inchangée
octet pour octet), et `PsqlDriver` sur un faux `psql` 16 qui refuse
l'option, un faux 17 qui l'accepte, et un faux à version illisible.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from ameesh import config as config_mod
from ameesh import db as db_mod
from ameesh import dsn as dsn_mod

# One record per call, the arguments of one call per line; `--version` is
# recorded too, some tests count it as the absence/presence of the probe.
# A libpq 16 (or an unreadable version: fail closed) refuses the option, a
# 17 accepts it.
FAKE_PSQL = r'''#!/bin/sh
printf '%s\n' "$*" >> "$FAKE_PSQL_LOG"
if [ "$1" = "--version" ]; then
    printf '%s\n' "$FAKE_PSQL_VERSION"
    exit 0
fi
case " $* " in
  *sslnegotiation*)
    major=$(printf '%s\n' "$FAKE_PSQL_VERSION" | sed -n 's/.*(PostgreSQL) \([0-9][0-9]*\).*/\1/p')
    if [ -z "$major" ] || [ "$major" -lt 17 ]; then
        printf 'psql: error: invalid URI query parameter: "sslnegotiation"\n' >&2
        exit 2
    fi ;;
esac
printf 't\n'
'''

URI_DSN = "postgresql://ameesh@10.77.0.1:5432/ameesh?sslmode=require&sslnegotiation=direct"
KEYWORD_DSN = "host=10.77.0.1 dbname=ameesh user=ameesh sslmode=require sslnegotiation=direct"


class WithoutParameterTest(unittest.TestCase):
    def test_uri_drops_the_parameter(self):
        self.assertEqual(
            dsn_mod.without_parameter(URI_DSN, "sslnegotiation"),
            "postgresql://ameesh@10.77.0.1:5432/ameesh?sslmode=require")
        # the userinfo (password included) is not this helper's business:
        # the caller ran split_password first, and this one only removes a key
        self.assertEqual(
            dsn_mod.without_parameter(
                "postgresql://u:Pw@h.invalid/db?sslnegotiation=direct&sslmode=require",
                "sslnegotiation"),
            "postgresql://u:Pw@h.invalid/db?sslmode=require")

    def test_uri_key_is_percent_decoded_as_libpq_reads_it(self):
        for key in ("ssl%6Eegotiation", "sslnegotiation"):
            with self.subTest(key=key):
                self.assertEqual(
                    dsn_mod.without_parameter(
                        "postgresql://u@h.invalid/db?%s=direct&sslmode=require" % key,
                        "sslnegotiation"),
                    "postgresql://u@h.invalid/db?sslmode=require")

    def test_keyword_form(self):
        self.assertEqual(
            dsn_mod.without_parameter(KEYWORD_DSN, "sslnegotiation"),
            "host=10.77.0.1 dbname=ameesh user=ameesh sslmode=require")
        # a quoted value survives the rewrite, and the removed key is gone
        self.assertEqual(
            dsn_mod.without_parameter(
                "host=h.invalid dbname='my db' sslnegotiation=direct", "sslnegotiation"),
            "host=h.invalid dbname='my db'")

    def test_noop_is_byte_for_byte(self):
        for dsn in ("postgresql://u@h.invalid/db?sslmode=require&",
                    "postgresql://u@h.invalid/db?sslnegotiation=direct&",
                    "host=h.invalid dbname=db", "ameesh", ""):
            with self.subTest(dsn=dsn):
                self.assertEqual(dsn_mod.without_parameter(dsn, "absent-key"), dsn)
        # removing the only parameter leaves no dangling "?"
        self.assertEqual(
            dsn_mod.without_parameter(
                "postgresql://u@h.invalid/db?sslnegotiation=direct&", "sslnegotiation"),
            "postgresql://u@h.invalid/db")

    def test_uri_without_a_path(self):
        """#5654 : la requête commence au premier « ? » sans chemin avant, même
        quand elle contient elle-même un « / »."""
        cases = {
            "postgresql://h.invalid:5432?sslnegotiation=direct&sslmode=require":
                "postgresql://h.invalid:5432?sslmode=require",
            "postgresql://h.invalid?sslrootcert=/x/ca.pem&sslnegotiation=direct":
                "postgresql://h.invalid?sslrootcert=/x/ca.pem",
            "postgresql://u@h.invalid?sslnegotiation=direct": "postgresql://u@h.invalid",
            "postgres://?host=/run/pg&sslnegotiation=direct": "postgres://?host=/run/pg",
        }
        for dsn, expected in cases.items():
            with self.subTest(dsn=dsn):
                self.assertEqual(dsn_mod.without_parameter(dsn, "sslnegotiation"), expected)
                self.assertEqual(dsn_mod.without_parameter(dsn, "absent-key"), dsn)

    def test_a_malformed_uri_stays_refused(self):
        with self.assertRaises(dsn_mod.DsnError):
            dsn_mod.without_parameter(
                "postgresql://u@h.invalid/db?a=1=2&sslnegotiation=direct", "sslnegotiation")


class PsqlDriverOptionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-5652-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        self.log = os.path.join(self.tmp, "calls.log")
        psql = os.path.join(bin_dir, "psql")
        with open(psql, "w", encoding="utf-8") as fh:
            fh.write(FAKE_PSQL)
        os.chmod(psql, 0o755)
        self.env = mock.patch.dict(os.environ, {
            "PATH": bin_dir + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_PSQL_LOG": self.log,
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def calls(self):
        with open(self.log, encoding="utf-8") as fh:
            return fh.read().splitlines()

    def driver(self, dsn, version):
        with mock.patch.dict(os.environ, {"FAKE_PSQL_VERSION": version}):
            return db_mod.PsqlDriver(config_mod.Config(dsn=dsn, driver="psql"))

    def test_libpq_16_no_longer_refuses_the_shared_dsn(self):
        """Le cas du constat : psql 16 + DSN écrit pour libpq 17."""
        db = self.driver(URI_DSN, "psql (PostgreSQL) 16.15 (Ubuntu 16.15)")
        self.assertEqual(db.name, "psql")
        sent = [line for line in self.calls() if not line.startswith("--version")]
        self.assertEqual(len(sent), 1, sent)
        self.assertIn("sslmode=require", sent[0])
        self.assertNotIn("sslnegotiation", sent[0])

    def test_libpq_16_keyword_form_too(self):
        self.driver(KEYWORD_DSN, "psql (PostgreSQL) 16.15 (Ubuntu 16.15)")
        sent = [line for line in self.calls() if not line.startswith("--version")]
        self.assertTrue(sent)
        self.assertNotIn("sslnegotiation", sent[0])

    def test_libpq_17_keeps_the_option(self):
        self.driver(URI_DSN, "psql (PostgreSQL) 17.2 (Debian 17.2-1)")
        sent = [line for line in self.calls() if not line.startswith("--version")]
        self.assertIn("sslnegotiation=direct", sent[0])

    def test_unreadable_version_does_not_change_the_dsn(self):
        # fail closed: the option is not dropped on a guess; the client still
        # refuses it, as before the fix
        with self.assertRaises(db_mod.Unavailable):
            self.driver(URI_DSN, "version inconnue")
        sent = [line for line in self.calls() if not line.startswith("--version")]
        self.assertTrue(sent)
        self.assertIn("sslnegotiation=direct", sent[0])

    def test_percent_encoded_key_is_dropped_too(self):
        """#5654 : le préfiltre lit les clés décodées, pas la sous-chaîne."""
        self.driver("postgresql://u@h.invalid/db?ssl%6Eegotiation=direct&sslmode=require",
                    "psql (PostgreSQL) 16.15 (Ubuntu 16.15)")
        self.assertEqual(sum(line.startswith("--version") for line in self.calls()), 1)
        sent = [line for line in self.calls() if not line.startswith("--version")]
        self.assertTrue(sent)
        self.assertNotIn("egotiation", sent[0])
        self.assertIn("sslmode=require", sent[0])

    def test_no_probe_when_the_name_is_only_a_value(self):
        # « sslnegotiation » ailleurs que dans une clé ne déclenche pas la sonde
        # (le faux psql refuse toute chaîne qui contient le mot : seul compte
        # ici l'absence de --version)
        with self.assertRaises(db_mod.Unavailable):
            self.driver("postgresql://u@h.invalid/sslnegotiation?sslmode=require",
                        "psql (PostgreSQL) 16.15 (Ubuntu 16.15)")
        self.assertFalse([line for line in self.calls() if line.startswith("--version")])

    def test_no_probe_without_a_concerned_option(self):
        self.driver("postgresql://ameesh@h.invalid/ameesh?sslmode=require",
                    "psql (PostgreSQL) 16.15 (Ubuntu 16.15)")
        self.assertEqual(len(self.calls()), 1, self.calls())


if __name__ == "__main__":
    unittest.main()
