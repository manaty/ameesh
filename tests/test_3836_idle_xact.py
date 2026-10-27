# SPDX-License-Identifier: AGPL-3.0-only
"""#3836 — garde serveur contre une session restée `idle in transaction`.

Le 27/10, le gel #3700 a laissé une transaction ouverte sept minutes : elle
tenait le verrou du dépôt de migration et le verrou consultatif du projet, et
les écritures du mesh attendaient. Le correctif de code interdit le réseau
dans la transaction du gel (`review_authors.reading(..., fetch=False)` et un
rapatriement avant la transaction) ; la garde serveur
`idle_in_transaction_session_timeout` borne tout client fautif, y compris
antérieur au correctif. Ces témoins couvrent la lecture du doctor et les deux
scripts de déploiement qui posent la garde — sans l'appliquer à chaud.
"""
from __future__ import annotations

import os
import unittest

from ameesh import hostcheck


class _Db:
    def __init__(self, setting=0, source="default", role="ameesh",
                 database="ameesh"):
        self.rows = [{"role": role, "database": database,
                      "setting": str(setting), "source": source}]

    def query(self, sql, params=()):
        return self.rows


class IdleTransactionDoctorTest(unittest.TestCase):
    def test_zero_is_a_warning_with_the_two_deployment_gestures(self):
        lines = hostcheck.idle_transaction_lines(_Db(0))
        self.assertTrue(lines[0].startswith("warning"), lines)
        self.assertIn('ALTER ROLE "ameesh" SET idle_in_transaction_session_timeout'
                      " = '180s'", lines[0])
        self.assertIn('ALTER DATABASE "ameesh" SET idle_in_transaction_session_timeout',
                      lines[0])

    def test_three_minutes_is_reported_as_the_guard(self):
        self.assertEqual(
            hostcheck.idle_transaction_lines(_Db(180000, source="role")),
            ["idle xact  : 180s (source role)"])

    def test_above_the_ceiling_is_still_a_warning(self):
        lines = hostcheck.idle_transaction_lines(_Db(600000, source="database"))
        self.assertTrue(lines[0].startswith("warning"), lines)
        self.assertIn("600s", lines[0])


class DeployGuardTest(unittest.TestCase):
    def _read(self, *parts):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, *parts), encoding="utf-8") as handle:
            return handle.read()

    def test_the_update_reapplies_the_guard_on_role_and_database(self):
        text = self._read("deploy", "mise-a-jour", "poste.sh")
        self.assertIn("reapply_idle_in_transaction_guard", text)
        self.assertIn("ALTER ROLE ameesh SET idle_in_transaction_session_timeout", text)
        self.assertIn("ALTER DATABASE ameesh SET idle_in_transaction_session_timeout",
                      text)
        self.assertIn("180s", text)

    def test_the_installer_sets_the_guard_on_role_and_database(self):
        text = self._read("deploy", "serveur", "installer-serveur.sh")
        self.assertIn("ALTER ROLE ameesh SET idle_in_transaction_session_timeout"
                      " = '180s'", text)
        self.assertIn("ALTER DATABASE ameesh SET idle_in_transaction_session_timeout"
                      " = '180s'", text)

    def test_the_installer_comment_matches_the_three_minute_guard(self):
        # N1 revue #3836 : le commentaire disait « 2 min » pour 180 s.
        text = self._read("deploy", "serveur", "installer-serveur.sh")
        self.assertIn("garde serveur la coupe à 3 min", text)
        self.assertNotIn("coupe à 2 min", text)


if __name__ == "__main__":
    unittest.main()
