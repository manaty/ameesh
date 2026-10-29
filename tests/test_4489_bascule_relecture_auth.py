# SPDX-License-Identifier: AGPL-3.0-only
"""#4489 — la validation finale de `accounts.choose` relit l'échec
d'authentification, comme les retenues et les identités (#1883).

Un échec publié par un autre runner de l'hôte entre la lecture de `sick` et
la bascule (ou le retour du compte actif) ne fait pas partir un tour sur ce
compte : la reprise saute au suivant sain, ou rend `auth_blocked`.
"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from ameesh import accounts, auth_failure, cost, storage

from .test_l226b_auth_failure import _AuthBase


class RelectureAuthTest(_AuthBase):

    def _items(self):
        return accounts.parse({"codex": self._codex_comptes()})["codex"]

    def _book(self):
        return cost.CostBook(state_dir=self.cfg.state_dir, db=self.db)

    def test_echec_publie_apres_le_choix_du_compte_actif(self):
        """Le compte actif est choisi sain ; son échec, publié avant la
        validation, fait partir le tour sur le suivant sain."""
        items = self._items()
        now = time.time()
        storage.of(self.db).accounts.init_active(self.cfg.host, "codex", "primaire")
        vrai = auth_failure.failing
        lectures = []

        def failing(db, host, harness):
            lectures.append(harness)
            if len(lectures) == 2:
                self._report("codex", "primaire", "failing", "turn")
            return vrai(db, host, harness)

        with mock.patch.object(auth_failure, "failing", failing):
            choix = accounts.choose(self.db, self.cfg.host, "codex", items,
                                    self._book(), now=now)
        self.assertEqual(choix.profile.name, "secondaire", choix.why)
        self.assertFalse(choix.auth_blocked)
        self.assertIn("primaire authentication failing", choix.why)
        self.assertEqual(len(lectures), 3)

    def test_echec_publie_pendant_la_bascule(self):
        """La cible de la bascule tombe en échec juste après le commit :
        aucun tour ne part sur elle ; sans autre compte sain, `auth_blocked`."""
        items = self._items()
        now = time.time()
        store = storage.of(self.db).accounts
        store.init_active(self.cfg.host, "codex", "primaire")
        self._report("codex", "primaire", "failing", "turn")
        vrai = type(store).switch_guarded

        def bascule_puis_echec(pilote, host, harness, *, to, **kw):
            resultat = vrai(pilote, host, harness, to=to, **kw)
            if resultat and to == "secondaire":
                self._report("codex", "secondaire", "failing", "turn")
            return resultat

        with mock.patch.object(type(store), "switch_guarded", bascule_puis_echec):
            choix = accounts.choose(self.db, self.cfg.host, "codex", items,
                                    self._book(), now=now)
        self.assertTrue(choix.auth_blocked, choix.why)
        self.assertIn("no healthy codex account", choix.why)

    def test_sans_echec_une_seule_relecture(self):
        """Sans changement d'état, une seule relecture : le choix est rendu."""
        items = self._items()
        vrai = auth_failure.failing
        lectures = []

        def failing(db, host, harness):
            lectures.append(harness)
            return vrai(db, host, harness)

        with mock.patch.object(auth_failure, "failing", failing):
            choix = accounts.choose(self.db, self.cfg.host, "codex", items,
                                    self._book(), now=time.time())
        self.assertEqual(choix.profile.name, "primaire", choix.why)
        self.assertEqual(len(lectures), 2)


if __name__ == "__main__":
    unittest.main()
