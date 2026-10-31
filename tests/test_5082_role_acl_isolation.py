# SPDX-License-Identifier: AGPL-3.0-only
"""Le verrou des contrats couvre la classe et se libère au cleanup."""
from .role_acl import serialize_role_acl
from .support import PgTestCase


def lock_holders(db) -> list[int]:
    return [row["pid"] for row in db.query(
        "SELECT pid FROM pg_locks l, pg_database d WHERE l.locktype = 'advisory' "
        "AND l.granted AND l.objsubid = 1 AND l.database = d.oid "
        "AND d.datname = current_database() "
        "AND (l.classid::bigint << 32 | l.objid::bigint) = "
        "hashtextextended('ameesh-test-role-public-acl', 0)")]


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
                holders = lock_holders(probe)
                self.assertEqual(len(holders), 1)
                raise RuntimeError("échec simulé de setUpClass")
        finally:
            for cleanup in reversed(Fixture.cleanups):
                cleanup()
        # Pas de nouvel essai de verrou : une autre classe en file le prend
        # dès la libération, seule compte la session de cette garde.
        self.assertNotIn(holders[0], lock_holders(probe))
