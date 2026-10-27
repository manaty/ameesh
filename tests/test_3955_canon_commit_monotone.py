# SPDX-License-Identifier: AGPL-3.0-only
"""#3955 : le commit audité (`canon_state.last_good_commit`) ne recule jamais.

Constat du 27/10 sur agents-2 : une synchronisation qui avait lu `origin/main`
AVANT le fetch du pilote a écrit son ancien commit APRÈS que la synchro du
pilote avait enregistré le nouveau. La porte du train Nexlink a relu l'ancien
commit pendant une minute et refusé de composer.

Attendu : l'avance du commit audité est conditionnelle (CAS sur la valeur
enregistrée) et gardée par l'ascendance git ; seul un retour arrière DÉCLARÉ
(`--allow-rewind`), journalisé au fil, peut faire reculer le commit.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ameesh import canon, canon_sync, fil, storage

from .support import PgTestCase
from .test_canon import EXAMPLE, _TmpMixin, commit_all, publish, write

HOST = "atelier"


class CanonCommitMonotoneTest(_TmpMixin, PgTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM canon_state")
        self.workspace = self.make_tmp()
        self.bare, self.clone = publish(self.workspace, "acme", EXAMPLE)
        self.c1 = canon.load(self.clone).sources[0].commit

    def sync(self, *args, **kwargs) -> canon_sync.SyncReport:
        return canon_sync.sync(self.db, canon.load(self.clone), HOST, *args, **kwargs)

    def change(self) -> str:
        write(self.clone, "note-3955.md", "note de course\n")
        return commit_all(self.clone, "note de course")

    def last_good(self) -> str:
        return (canon_sync.state(self.db, HOST) or {}).get("last_good_commit")

    def thread_texts(self) -> list[str]:
        entry = fil.ThreadRef(fil.project_for(self.cfg))
        return [e.text for e in fil.transport_for(self.cfg).read(entry)]

    def test_synchros_entrelacees_le_commit_audite_reste_le_plus_recent(self):
        """Témoin : A lit la ref avant le fetch, B enregistre le nouveau commit
        après son fetch, A écrit ensuite son ancien commit — il ne recule pas."""
        self.sync(trusted_ref="origin/main")
        stale = canon.load(self.clone)           # A : ref lue avant le fetch
        c2 = self.change()
        fresh = canon.load(self.clone)           # B : ref lue après le fetch
        raced = []
        original = canon_sync.record_state

        def interleave(db, host, status, diagnostic, canon=None, *, key=None, **kwargs):
            # B s'intercale juste avant l'écriture de l'état par A, comme le
            # sync du pilote pendant la passe d'un runner chargé.
            if (status == canon_sync.CANON_OK and canon is not None
                    and canon.sources and canon.sources[0].commit == self.c1
                    and not raced):
                raced.append(True)
                done = canon_sync.sync(db, fresh, host, trusted_ref="origin/main")
                self.assertEqual(done.status, canon_sync.CANON_OK)
            return original(db, host, status, diagnostic, canon, key=key, **kwargs)

        with mock.patch.object(canon_sync, "record_state", side_effect=interleave):
            report = canon_sync.sync(self.db, stale, HOST, trusted_ref="origin/main")
        self.assertEqual(raced, [True])
        self.assertEqual(self.last_good(), c2)
        self.assertEqual(report.status, canon_sync.CANON_OK)
        # A, en retard, ne réécrit pas non plus le journal des authentificateurs
        self.assertEqual(report.authenticators.status, canon_sync.AUTH_REFUSED)
        # l'état du commit audité est bien celui de B, pas celui de A
        self.assertIn(c2[:12], canon_sync.state(self.db, HOST)["source"])

    def test_une_synchro_en_retard_ne_recule_pas(self):
        """Sans entrelacement : le sync en retard garde l'état enregistré."""
        self.sync()
        stale = canon.load(self.clone)
        c2 = self.change()
        self.sync()
        canon_sync.record_state(self.db, HOST, canon_sync.CANON_OK, "", stale)
        self.assertEqual(self.last_good(), c2)

    def test_ecriture_conditionnelle_detecte_une_avance_concurrente(self):
        """Le CAS de `record_state` : une écriture fondée sur une lecture
        périmée laisse le commit déjà avancé par une autre synchro."""
        self.sync()
        stale = canon.load(self.clone)
        c2 = self.change()
        fresh = canon.load(self.clone)
        store = storage.of(self.db).canon
        # B enregistre C2 entre la lecture de A (C1) et son écriture.
        written = store.record_state(
            HOST, canon_sync.CANON_OK, root=fresh.root, source=fresh.source_label(),
            commit=c2, good=True, diagnostic="", canon="", expected=self.c1)
        self.assertEqual(written, c2)
        written = store.record_state(
            HOST, canon_sync.CANON_OK, root=stale.root, source=stale.source_label(),
            commit=self.c1, good=True, diagnostic="", canon="", expected=self.c1)
        self.assertEqual(written, c2)            # CAS perdu : C1 n'a pas écrasé C2
        self.assertEqual(self.last_good(), c2)

    def test_retour_arriere_declare_est_accepte_et_journalise(self):
        self.sync()
        stale = canon.load(self.clone)
        c2 = self.change()
        self.sync()
        self.assertEqual(self.last_good(), c2)
        canon_sync.record_state(self.db, HOST, canon_sync.CANON_OK, "", stale,
                                allow_rewind=True)
        self.assertEqual(self.last_good(), self.c1)
        entries = self.thread_texts()
        self.assertTrue(any("DECLARED REWIND" in text
                            and self.c1[:12] in text and c2[:12] in text
                            for text in entries), entries)

    def test_retour_arriere_non_declare_refuse(self):
        self.sync()
        stale = canon.load(self.clone)
        c2 = self.change()
        self.sync()
        canon_sync.record_state(self.db, HOST, canon_sync.CANON_OK, "", stale)
        self.assertEqual(self.last_good(), c2)
        self.assertFalse(any("DECLARED REWIND" in text for text in self.thread_texts()))


if __name__ == "__main__":
    unittest.main()
