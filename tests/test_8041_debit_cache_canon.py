# SPDX-License-Identifier: AGPL-3.0-only
"""#8041 : `debit.read` lit chaque canon une fois par lecture.

Témoin du 22/11 (analyse de nuit de l'auditeur) : `ameesh debit --json`
durait 49,6 s, dont 29,8 s à recharger les canons 56 fois — chaque contrôle
d'autorité (`assignments.is_orchestrator`, `account_turn._is_critical`)
relisait tous les canons hors de toute fenêtre `canon.configured_cache`.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import canon, debit


class DebitCanonCacheTest(unittest.TestCase):
    def test_un_canon_n_est_lu_qu_une_fois_par_lecture(self):
        def lecture(db, **kwargs):
            # deux contrôles d'autorité successifs, comme planned_waits puis
            # account_turn : le même canon demandé deux fois
            canon.load("/canon", ref="main")
            canon.load("/canon", ref="main")
            return {"lu": True}

        with mock.patch.object(canon, "_load", return_value=object()) as charge, \
                mock.patch.object(debit, "_read", side_effect=lecture):
            self.assertEqual(debit.read(None), {"lu": True})
        self.assertEqual(charge.call_count, 1)

    def test_la_fenetre_ne_survit_pas_a_la_lecture(self):
        avant = getattr(canon._CANONS, "loaded", None)
        with mock.patch.object(debit, "_read", return_value={}):
            debit.read(None)
        self.assertIs(getattr(canon._CANONS, "loaded", None), avant)
        with mock.patch.object(debit, "_read", side_effect=RuntimeError("panne")):
            with self.assertRaises(RuntimeError):
                debit.read(None)
        self.assertIs(getattr(canon._CANONS, "loaded", None), avant)

    def test_les_arguments_passent_tels_quels(self):
        with mock.patch.object(debit, "_read", return_value={}) as interne:
            debit.read("db", cfg="cfg", now=1.0, hours=3, wait_s=4.0,
                       ci_hours=5, ci_slow_s=6.0, canons=["c"],
                       installed="1.0", jobs=False, api="api")
        interne.assert_called_once_with(
            "db", cfg="cfg", now=1.0, hours=3, wait_s=4.0, ci_hours=5,
            ci_slow_s=6.0, canons=["c"], installed="1.0", jobs=False,
            api="api")


if __name__ == "__main__":
    unittest.main()
