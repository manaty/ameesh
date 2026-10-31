# SPDX-License-Identifier: AGPL-3.0-only
"""Le verrou des contrats couvre la classe et se libère au cleanup."""
from .role_acl import serialize_role_acl
from .support import PgTestCase


class RoleAclIsolationTest(PgTestCase):
    def test_lock_survives_statements_and_setup_failure(self):
        class Fixture:
            cfg = self.cfg
            cleanups = []

            @classmethod
            def addClassCleanup(cls, cleanup):
                cls.cleanups.append(cleanup)

        probe = self.connect()
        self.addCleanup(probe.close)
        try:
            with self.assertRaisesRegex(RuntimeError, "échec simulé"):
                serialize_role_acl(Fixture)
                # Les scripts sont lancés sur d'autres connexions ; une requête
                # terminée sur la connexion de classe ne doit pas lever la garde.
                self.db.query("SELECT 1")
                with probe.transaction() as tx:
                    self.assertFalse(tx.query(
                        "SELECT pg_try_advisory_xact_lock(hashtextextended("
                        "'ameesh-test-role-public-acl', 0)) AS acquired")[0]["acquired"])
                raise RuntimeError("échec simulé de setUpClass")
        finally:
            for cleanup in reversed(Fixture.cleanups):
                cleanup()
        with probe.transaction() as tx:
            self.assertTrue(tx.query(
                "SELECT pg_try_advisory_xact_lock(hashtextextended("
                "'ameesh-test-role-public-acl', 0)) AS acquired")[0]["acquired"])
