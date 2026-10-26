# SPDX-License-Identifier: AGPL-3.0-only
"""#3631 — la base commune nomme les requêtes qui expirent.

Un `statement_timeout` ne dit pas, à lui seul, quelle instruction a expiré :
le pilote attache désormais l'instruction et sa durée à `DbError`, et la
passe de redésignation nomme la lecture qui l'a perdue.
"""
from __future__ import annotations

import unittest

from ameesh import db as db_mod
from ameesh import review_designation_activation as act


class TimeoutDetailTest(unittest.TestCase):
    def test_names_the_statement_and_the_duration(self):
        exc = db_mod.DbError(
            "canceling statement due to statement timeout",
            sqlstate=db_mod.QUERY_CANCELED,
            sql="SELECT name, aliases, retired_ts FROM agent_registry",
            elapsed_ms=30012.0)
        text = db_mod.timeout_detail(exc)
        self.assertIn("statement timeout", text)
        self.assertIn("after 30.0s", text)
        self.assertIn("SELECT name, aliases", text)

    def test_a_statement_is_omitted_when_unknown(self):
        exc = db_mod.DbError(
            "canceling statement due to statement timeout",
            sqlstate=db_mod.QUERY_CANCELED)
        self.assertEqual(
            db_mod.timeout_detail(exc),
            "canceling statement due to statement timeout")

    def test_another_error_keeps_its_message(self):
        exc = db_mod.DbError("boom", sqlstate="42P01", sql="SELECT 1")
        self.assertEqual(db_mod.timeout_detail(exc), "boom")

    def test_the_message_of_a_timeout_is_unchanged(self):
        # aucune classification ne dépend du texte : l'ajout est porté par des
        # attributs, pas par le message (codex #478 r2).
        exc = db_mod.DbError(
            "canceling statement due to statement timeout",
            sqlstate=db_mod.QUERY_CANCELED,
            sql="SELECT 1")
        self.assertEqual(str(exc),
                         "canceling statement due to statement timeout")


class NamedReadTest(unittest.TestCase):
    def test_a_failure_names_the_read(self):
        exc = db_mod.DbError(
            "canceling statement due to statement timeout",
            sqlstate=db_mod.QUERY_CANCELED, sql="SELECT name FROM agent_registry")

        def boom():
            raise exc

        with self.assertRaises(act._UnreadableInputs) as ctx:
            act._named_read("agents.reachability", boom)
        self.assertIn("agents.reachability", str(ctx.exception))
        self.assertIn("statement timeout", str(ctx.exception))
        self.assertIn("SELECT name FROM agent_registry", str(ctx.exception))

    def test_a_success_returns_the_value(self):
        self.assertEqual(act._named_read("x", lambda: 7), 7)


if __name__ == "__main__":
    unittest.main()
