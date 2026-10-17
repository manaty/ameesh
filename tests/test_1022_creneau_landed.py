# SPDX-License-Identifier: AGPL-3.0-only
"""#1022 : une fusion lande le creneau de migration, une liberation sans
atterrissage le rend `released` (suite de #711, ordonne avec #956).
#879 : un numero deja atterri ne redevient jamais `reserved`."""
from __future__ import annotations

import os

from ameesh import work

from .support import PgTestCase
from .test_l122_numeros_de_migration import _depot, NEXLINK


class MigrationSlotLandedTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.repo = _depot(os.path.join(self.tmp, 'repo'), 'manaty/nexlink', NEXLINK.directory,
                           ['375_base.sql', '376_base.sql'], {})
        self.lot = work.add(self.db, title='creneau', issue_ref='manaty/nexlink#1', app='nexlink',
                            actor='mesh-design')
        work.set_branch(self.db, self.lot['id'], 'agent/creneau', target='origin/develop',
                        actor='dev')
        work.reserve_migration(self.db, self.lot['id'], 400, repo=self.repo, actor='dev')

    def _creneau(self) -> dict:
        rows = self.db.query('SELECT slot, number, status FROM work_migration_reservations'
                             ' ORDER BY slot')
        self.assertEqual([row['slot'] for row in rows], ['default'])
        return rows[0]

    def test_une_instruction_fusion_et_null_lande_le_creneau(self):
        """Le constat : un ecrivain fusionne et remet la projection a NULL
        dans la meme instruction (ou un client anterieur a 0419)."""
        self.assertEqual(self._creneau()['status'], 'reserved')
        self.db.execute("UPDATE work_items SET state = 'merged', migration = NULL,"
                        " migration_repo = NULL WHERE id = %s", (self.lot['id'],))
        self.assertEqual(self._creneau()['status'], 'landed')

    def test_deux_instructions_fusion_puis_null_lande_le_creneau(self):
        """Le chemin de #956 : transition puis mise a NULL, meme transaction."""
        with self.db.transaction() as tx:
            tx.execute("UPDATE work_items SET state = 'merged' WHERE id = %s", (self.lot['id'],))
            tx.execute("UPDATE work_items SET migration = NULL, migration_repo = NULL"
                       " WHERE id = %s", (self.lot['id'],))
        self.assertEqual(self._creneau()['status'], 'landed')

    def test_liberation_sans_atterrissage_reste_released(self):
        """`work migration --release` sur un lot ouvert : libere, pas lande."""
        self.db.execute("UPDATE work_items SET migration = NULL, migration_repo = NULL"
                        " WHERE id = %s", (self.lot['id'],))
        self.assertEqual(self._creneau()['status'], 'released')

    def test_cloture_sans_atterrissage_reste_released(self):
        """Un lot abandonne libere son creneau sans le lander."""
        self.db.execute("UPDATE work_items SET state = 'closed', close_reason = 'abandoned',"
                        " migration = NULL, migration_repo = NULL WHERE id = %s",
                        (self.lot['id'],))
        self.assertEqual(self._creneau()['status'], 'released')

    def test_merged_puis_blocked_ne_ressuscite_pas_le_creneau(self):
        """#879 : `merged -> blocked` rejoue la projection avec `migration`
        encore pose ; un `landed` de meme numero le reste."""
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s", (self.lot['id'],))
        self.assertEqual(self._creneau()['status'], 'landed')
        self.db.execute("UPDATE work_items SET state = 'blocked' WHERE id = %s", (self.lot['id'],))
        self.assertEqual(self._creneau()['status'], 'landed')

    def test_nouveau_numero_apres_landed_reste_reserve(self):
        """#879 : la garde ne fige pas la projection — un numero nouveau suit
        son statut normal (`reserved`)."""
        self.db.execute("UPDATE work_items SET state = 'merged' WHERE id = %s", (self.lot['id'],))
        self.assertEqual(self._creneau()['status'], 'landed')
        self.db.execute("UPDATE work_items SET state = 'blocked', migration = 401"
                        " WHERE id = %s", (self.lot['id'],))
        creneau = self._creneau()
        self.assertEqual((creneau['number'], creneau['status']), (401, 'reserved'))
