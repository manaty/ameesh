# SPDX-License-Identifier: AGPL-3.0-only
"""L539 — le lot de relecture ouvert après la fusion d'un lot léger (§2).

Deux niveaux : le module seul (magasin simulé) pour la règle — seule la classe
`light` ouvre, une seule fois, sans assigné, échéance du jour — puis sur le
vrai Postgres pour l'écriture (lot `intake` sans assigné, `issue_ref` et
marque au journal, idempotence)."""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest import mock

from ameesh import registry, review_lot, storage, work

from .support import PgTestCase


class FakeOps:
    """Le magasin `work` en mémoire, pour la règle seule."""

    def __init__(self, items=None):
        self.items = dict(items or {})
        self.notes: list = []
        self.added: list = []
        self.events_rows: dict = {}
        self.next_id = 900

    def open_by_ref(self, ref):
        return [i for i in self.items.values()
                if i.get("issue_ref") == ref
                and i.get("state") not in ("merged", "promoted", "closed")]

    def by_issue_ref(self, ref):
        """L'identité du lot de relecture, quel que soit son état (revue #544)."""
        return [i for i in self.items.values() if i.get("issue_ref") == ref]

    def events(self, item_id, limit):
        return self.events_rows.get(int(item_id), [])

    def note(self, item_id, state, text, actor):
        self.notes.append((int(item_id), text, actor))

    def add(self, **kw):
        self.next_id += 1
        item = dict(kw, id=self.next_id, state="intake", assignee=None)
        self.items[self.next_id] = item
        self.added.append(item)
        return item


LOT = 539


class RuleTest(unittest.TestCase):
    """La règle : classe, échéance, famille opposée, une seule ouverture."""

    def setUp(self):
        self.item = {"id": LOT, "title": "un test de chemin léger", "app": "ameesh",
                     "assignee": "a1", "state": "merged", "branch_target": "main",
                     "type": "evolution"}
        self.ops = FakeOps({LOT: self.item})
        store = mock.Mock(work=self.ops)
        for patch in (
            mock.patch.object(storage, "of", return_value=store),
            mock.patch.object(work, "get",
                              side_effect=lambda db, n: self.ops.items.get(int(n))),
            mock.patch.object(work, "add", side_effect=lambda db, **kw: self.ops.add(**kw)),
            mock.patch.object(work, "note",
                              side_effect=lambda db, n, text, actor="": self.ops.note(
                                  n, "", text, actor)),
            mock.patch.object(registry, "get",
                              side_effect=lambda db, name: {"harness": "deepseek"}
                              if name == "a1" else None),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def open(self, klass=None, **kw):
        return review_lot.open_after_merge(
            None, lot=LOT,
            klass=klass if klass is not None else {"class": "light", "why": "policy of ameesh"},
            **kw)

    def test_seule_la_classe_light_ouvre(self):
        for klass in ({"class": "sensitive"}, {"class": "release"}, {"class": "normal"}, {}):
            out = review_lot.open_after_merge(None, lot=LOT, klass=klass)
            self.assertFalse(out["created"], (klass, out))
            self.assertIn("pas de lot de relecture", out["why"])
        self.assertEqual(self.ops.added, [])

    def test_un_lot_light_ouvre_un_lot_sans_assigne(self):
        out = self.open(merged_sha="a" * 40, pr=476)
        self.assertTrue(out["created"], out)
        (item,) = self.ops.added
        self.assertEqual(item["state"], "intake")
        self.assertIsNone(item["assignee"])
        self.assertEqual(item["type"], "evolution")
        self.assertEqual(item["issue_ref"], review_lot.ref_of(LOT))
        self.assertEqual(item["source"], review_lot.ref_of(LOT))
        self.assertEqual(item["app"], "ameesh")
        self.assertIn("#539", item["title"])
        body = item["body"]
        self.assertIn("23:59 Europe/Paris", body)
        self.assertIn("codex", body)           # famille opposée à deepseek
        self.assertIn("a" * 12, body)
        self.assertIn("ameesh work milestone 539 verdict", body)

    def test_la_seconde_ouverture_est_un_no_op(self):
        first = self.open()
        second = self.open()
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self.ops.added), 1)

    def test_la_marque_au_journal_suffit_si_le_lot_est_ferme(self):
        """Un lot de relecture clos ne rouvre pas : la ligne de journal le dit."""
        self.ops.events_rows[LOT] = [
            {"note": "review-after-merge:#539 lot #901 ouvert (classe light, relecture due …)"}]
        out = self.open()
        self.assertFalse(out["created"], out)
        self.assertIn("marqué", out["why"])
        self.assertEqual(self.ops.added, [])

    def test_un_lot_de_relecture_ferme_sans_marque_ne_rouvre_pas(self):
        """Revue #544 r1, cas reproduit : marque absente (écriture ratée), lot
        de relecture fermé, nouvel appel — l'identité tient à la `issue_ref`,
        pas à l'état ni à la marque au journal."""
        first = self.open()
        self.ops.items[first["id"]]["state"] = "merged"
        self.ops.events_rows.pop(LOT, None)
        out = self.open()
        self.assertFalse(out["created"], out)
        self.assertEqual(out["id"], first["id"])
        self.assertEqual(len(self.ops.added), 1)

    def test_rien_n_est_ecrit_quand_l_ecriture_echoue(self):
        with mock.patch.object(work, "add", side_effect=work.WorkError("refusé")):
            out = self.open()
        self.assertFalse(out["created"])
        self.assertIn("WorkError", out["why"])
        self.assertIn("refusé", out["why"])

    def test_echeance_et_familles(self):
        self.assertEqual(review_lot.deadline(datetime(2026, 10, 13, 2, 0)),
                         "2026-10-13 23:59 Europe/Paris")
        self.assertEqual(review_lot.reviewer_family("deepseek"), "codex")
        self.assertEqual(review_lot.reviewer_family("claude"), "codex")
        self.assertEqual(review_lot.reviewer_family("codex"), "claude")
        self.assertIn("codex", review_lot.reviewer_family(""))
        self.assertEqual(review_lot.ref_of(539), "review-after-merge:#539")


class ReviewLotDbTest(PgTestCase):
    """L'écriture réelle : lot ouvert, marque au journal, idempotence."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "a1", harness="deepseek", host=self.cfg.host)
        self.lot = work.add(self.db, title="un chemin léger", app="ameesh",
                            assignee="a1")["id"]

    def test_le_lot_de_relecture_est_ouvert_une_fois(self):
        out = review_lot.open_after_merge(
            self.db, lot=self.lot, klass={"class": "light", "why": "policy of ameesh"},
            merged_sha="b" * 40, pr=476, target="main")
        self.assertTrue(out["created"], out)
        item = work.get(self.db, out["id"])
        self.assertEqual((item["state"], item["assignee"], item["type"], item["app"]),
                         ("intake", None, "evolution", "ameesh"))
        self.assertEqual(item["issue_ref"], review_lot.ref_of(self.lot))
        self.assertIn("23:59 Europe/Paris", item["body"])
        self.assertIn("codex", item["body"])
        events = storage.of(self.db).work.events(self.lot, 20)
        self.assertTrue(any(review_lot.ref_of(self.lot) in (e["note"] or "")
                            for e in events), events)
        again = review_lot.open_after_merge(
            self.db, lot=self.lot, klass={"class": "light", "why": "policy of ameesh"},
            merged_sha="b" * 40, pr=476)
        self.assertFalse(again["created"], again)
        self.assertEqual(again["id"], out["id"])
        self.assertEqual([i["id"] for i in storage.of(self.db).work.open_by_ref(
            review_lot.ref_of(self.lot))], [out["id"]])

    def test_la_marque_et_le_lot_sont_une_seule_ecriture(self):
        """Revue #544 r1 : l'échec de la marque annule la création.

        Seule la ligne de journal de la marque échoue ; sans la transaction
        unique, le lot de relecture resterait créé sans marque et un second
        appel (ou un journal tronqué) en ouvrirait un autre."""
        from ameesh.db import DbError
        from ameesh.storage.postgres.work import WorkItems as PgWorkItems
        before = self.db.query("SELECT count(*) AS n FROM work_items")[0]["n"]
        original = PgWorkItems._event

        def failing(self, item_id, state, note, actor):
            if str(note).startswith(review_lot.PREFIX):
                raise DbError("marque refusée")
            return original(self, item_id, state, note, actor)

        with mock.patch.object(PgWorkItems, "_event", failing):
            out = review_lot.open_after_merge(
                self.db, lot=self.lot, klass={"class": "light", "why": "policy"},
                merged_sha="d" * 40)
        self.assertFalse(out["created"], out)
        self.assertIn("non ouvert", out["why"])
        after = self.db.query("SELECT count(*) AS n FROM work_items")[0]["n"]
        self.assertEqual(after, before)
        self.assertEqual(storage.of(self.db).work.by_issue_ref(
            review_lot.ref_of(self.lot)), [])
        # l'incident passé, un seul lot de relecture est ouvert
        again = review_lot.open_after_merge(
            self.db, lot=self.lot, klass={"class": "light", "why": "policy"},
            merged_sha="d" * 40)
        self.assertTrue(again["created"], again)
        self.assertEqual([i["id"] for i in storage.of(self.db).work.by_issue_ref(
            review_lot.ref_of(self.lot))], [again["id"]])

    def test_un_lot_de_relecture_ferme_ne_rouvre_pas(self):
        """Revue #544 r1 : l'identité tient à la `issue_ref`, pas à l'état."""
        first = review_lot.open_after_merge(
            self.db, lot=self.lot, klass={"class": "light", "why": "policy"},
            merged_sha="e" * 40)
        self.assertTrue(first["created"], first)
        storage.of(self.db).work.move(first["id"], "merged", current="intake",
                                      loops=0, note="", actor="orch")
        self.db.execute("DELETE FROM work_item_events WHERE note LIKE %s",
                        (review_lot.PREFIX + "%",))
        again = review_lot.open_after_merge(
            self.db, lot=self.lot, klass={"class": "light", "why": "policy"},
            merged_sha="e" * 40)
        self.assertFalse(again["created"], again)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(storage.of(self.db).work.by_issue_ref(
            review_lot.ref_of(self.lot))), 1)

    def test_une_classe_non_light_n_ouvre_rien(self):
        for klass in ({"class": "sensitive"}, {"class": "release"}, {"class": "normal"}, {}):
            out = review_lot.open_after_merge(self.db, lot=self.lot, klass=klass,
                                              merged_sha="b" * 40)
            self.assertFalse(out["created"], (klass, out))
        self.assertEqual(
            storage.of(self.db).work.open_by_ref(review_lot.ref_of(self.lot)), [])

    def test_un_lot_inconnu_est_rendu_sans_erreur(self):
        out = review_lot.open_after_merge(self.db, lot=99999,
                                          klass={"class": "light"}, merged_sha="c" * 40)
        self.assertFalse(out["created"])
        self.assertIn("introuvable", out["why"])


if __name__ == "__main__":
    unittest.main()
