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
        self.milestones_rows: dict = {}
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

    def milestones(self, item_id, limit=100):
        return self.milestones_rows.get(int(item_id), [])

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
        # #1053 : la famille vient du gel (acteur + tours tracés), pas du registre
        self.ops.milestones_rows[LOT] = [
            {"id": 1, "kind": "frozen", "sha": "a" * 40, "actor": "a1",
             "at_ts": 1_700_000_000.0}]
        store = mock.Mock(work=self.ops)
        for patch in (
            mock.patch.object(storage, "of", return_value=store),
            mock.patch.object(work, "get",
                              side_effect=lambda db, n: self.ops.items.get(int(n))),
            mock.patch.object(work, "add", side_effect=lambda db, **kw: self.ops.add(**kw)),
            mock.patch.object(work, "note",
                              side_effect=lambda db, n, text, actor="": self.ops.note(
                                  n, "", text, actor)),
            mock.patch.object(review_lot, "_turns_harness", return_value="codex"),
            # #2664 : banc sans canon ; familles compilées épinglées.
            mock.patch.object(review_lot, "compiled_families",
                              return_value=("claude", "codex", "deepseek")),
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
        # #1053 : la famille suit les tours du gel (codex), pas le registre
        # courant (deepseek) — le relecteur désigné reste donc Claude.
        self.assertIn("- Auteur : a1 (harnais codex)", body)
        self.assertIn("— claude (règle du pilote du 13/10)", body)
        self.assertNotIn("codex ou claude", body)
        self.assertIn("a" * 12, body)
        self.assertIn("ameesh work milestone 539 verdict", body)
        # #1280 B2 : la mission dit au lecteur comment la clore après verdict
        self.assertIn("clore cette mission", body)
        self.assertIn("--done", body)

    def test_un_vehicule_de_paquet_n_ouvre_pas_de_relecture(self):
        """#4747 : les avis d'un porteur `integration/**` portent sur ses membres
        (§12) ; lui ouvrir une relecture après fusion dépensait un tour de
        relecteur par paquet léger fusionné (#4742 sur #4739)."""
        self.item["branch"] = "integration/paquet-h-20261030-01"
        out = self.open(merged_sha="a" * 40, pr=476)
        self.assertFalse(out["created"], out)
        self.assertIn("véhicule de paquet", out["why"])
        self.assertEqual(self.ops.added, [])

    def gate_klass(self):
        """La classe réelle de la porte de fusion avant le correctif : le canon
        ne nomme pas `merge_pr.py`, la politique le rend donc `light` (#2374)."""
        return {"class": "light",
                "why": "policy of ameesh; 2 file(s) since b791534b",
                "files": [
                    {"path": "src/ameesh/merge_pr.py", "class": "light", "rule": None},
                    {"path": "tests/test_2273_merge_pr_required.py", "class": "light",
                     "rule": None},
                ]}

    def test_un_light_sur_la_porte_de_fusion_ouvre_deux_avis(self):
        """#2374 : une classe light qui touche `CRITICAL_SCOPE` (porte de
        fusion) ouvre une relecture **sensible**, avec deux familles."""
        out = self.open(self.gate_klass(), merged_sha="b" * 40, pr=476)
        self.assertTrue(out["created"], out)
        self.assertEqual(out["class"], "sensitive", out)
        self.assertEqual(out["merged_class"], "light", out)
        self.assertTrue(out["critical"], out)
        self.assertEqual(out["reviews"], 2, out)
        body = self.ops.added[0]["body"]
        self.assertIn("**porte de fusion** : deux avis indépendants", body)
        self.assertIn("Classe : sensitive", body)
        self.assertIn("porte de fusion : chemin critique, deux avis requis", body)
        self.assertIn("deux familles distinctes", body)
        self.assertIn("claude et deepseek", body)
        self.assertIn("Avis requis : **2**", body)
        self.assertIn("livrer un verdict par relecteur", body)
        self.assertIn("second avis avant la clôture", body)
        self.assertNotIn("§2, lot léger", body)
        # #2384 : un auteur dont la famille est tracée n'a pas la route de sortie
        self.assertNotIn("Famille de l'auteur non tracée", body)

    def test_porte_de_fusion_sans_famille_auteur_explique_la_route(self):
        """#2384 : quand la famille de l'auteur n'est pas tracée, le corps ne
        peut pas promettre les deux avis sans dire que la garde refusera
        `--done` ; la route (pilote : abandon/remplacement ou dispense) est
        écrite avant les verdicts."""
        self.ops.milestones_rows.pop(LOT)
        out = self.open(self.gate_klass(), merged_sha="c" * 40)
        self.assertTrue(out["created"], out)
        body = self.ops.added[0]["body"]
        self.assertIn("harnais non tracé au gel", body)
        self.assertIn("**Famille de l'auteur non tracée**", body)
        self.assertIn("--done", body)
        self.assertIn("pilote", body)
        self.assertIn("--abandoned", body)
        self.assertIn("--superseded-by", body)
        self.assertIn("dispense", body)
        self.assertIn("route ci-dessus", body)
        self.assertIn("deux familles distinctes", body)
        self.assertIn("livrés les verdicts ? saisir le pilote", body)
        self.assertNotIn("clore cette mission** : `ameesh work close <id> --done`", body)

    def test_auteur_humain_sans_harnais_ne_promet_pas_un_refus(self):
        body = review_lot.body_of(
            self.item, LOT, klass={"class": "sensitive", "critical": True},
            author={"name": "human:smichea", "harness": ""},
            families=("claude", "codex", "deepseek"))
        self.assertNotIn("Famille de l'auteur non tracée", body)
        self.assertNotIn("sera refusé", body)
        self.assertIn("clore cette mission** : `ameesh work close <id> --done`", body)

    def test_tiers_qui_regele_ne_donne_pas_sa_famille_a_lauteur(self):
        body = review_lot.body_of(
            self.item, LOT, klass={"class": "sensitive", "critical": True},
            author={"name": "a1", "harness": "", "refroze": "claude1"},
            families=("claude", "codex", "deepseek"))
        self.assertIn("Famille de l'auteur non tracée", body)
        self.assertIn("codex et claude", body)
        self.assertNotIn("codex et deepseek", body)

    def test_un_light_hors_porte_de_fusion_reste_un_avis(self):
        """Le correctif ne transforme pas toute fusion light en relecture
        sensible : un chemin ordinaire garde un avis et le gabarit léger."""
        klass = {"class": "light", "why": "policy of ameesh",
                 "files": [{"path": "docs/guide.md", "class": "light", "rule": None}]}
        out = self.open(klass)
        self.assertTrue(out["created"], out)
        self.assertEqual(out["class"], "light", out)
        self.assertEqual(out["reviews"], 1, out)
        self.assertFalse(out["critical"], out)
        body = self.ops.added[0]["body"]
        self.assertIn("§2, lot léger", body)
        self.assertIn("une **autre famille de modèle**", body)
        self.assertNotIn("Avis requis", body)

    def test_la_porte_de_fusion_deja_ouverte_ne_rouvre_pas(self):
        first = self.open(self.gate_klass())
        again = self.open(self.gate_klass())
        self.assertTrue(first["created"], first)
        self.assertFalse(again["created"], again)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(self.ops.added), 1)

    def test_sans_tour_trace_la_famille_reste_inconnue(self):
        """#1053 : aucun tour au gel — le registre courant (deepseek) ne sert
        pas de preuve ; la famille reste ouverte, et le corps le dit."""
        self.ops.milestones_rows.pop(LOT)
        out = self.open()
        self.assertTrue(out["created"], out)
        body = self.ops.added[0]["body"]
        self.assertIn("harnais non tracé au gel", body)
        self.assertIn("codex ou claude", body)

    def test_un_avis_n_ouvre_pas_la_relecture_d_un_avis(self):
        """R codex3 #1280 B2 : un avis déclaré fusionné ne rouvre pas la
        relecture d'un avis — la garde coupe la récursion avant la création."""
        self.ops.items[LOT] = {
            "id": LOT, "title": "REVUE #12 : relecture après fusion",
            "source": "review-after-merge:#12",
            "issue_ref": "review-after-merge:#12",
            "state": "merged", "app": "ameesh"}
        out = self.open()
        self.assertFalse(out["created"], out)
        self.assertIn("avis", out["why"])
        self.assertEqual(self.ops.added, [])

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
        F = ("claude", "codex", "deepseek")
        self.assertEqual(review_lot.reviewer_family("deepseek", F), "codex")
        self.assertEqual(review_lot.reviewer_family("claude", F), "codex")
        self.assertEqual(review_lot.reviewer_family("codex", F), "claude")
        self.assertIn("codex", review_lot.reviewer_family("", F))
        # #2374 : deux familles distinctes, hors de l'auteur, pour la porte
        self.assertEqual(review_lot.review_families("deepseek", F), ("codex", "claude"))
        self.assertEqual(review_lot.review_families("claude", F), ("codex", "deepseek"))
        self.assertEqual(review_lot.review_families("codex", F), ("claude", "deepseek"))
        self.assertEqual(review_lot.review_families("", F), ("codex", "claude"))
        self.assertEqual(review_lot.ref_of(539), "review-after-merge:#539")


class ReviewLotDbTest(PgTestCase):
    """L'écriture réelle : lot ouvert, marque au journal, idempotence."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "a1", harness="deepseek", host=self.cfg.host)
        self.lot = work.add(self.db, title="un chemin léger", app="ameesh",
                            assignee="a1")["id"]
        # #1053 : le gel et le tour qui l'a produit portent la famille du lot
        self.db.execute("INSERT INTO turn_costs (agent, harness, recorded_at)"
                        " VALUES (%s, %s, now() - interval '1 minute')",
                        ("a1", "deepseek"))
        work.milestone(self.db, self.lot, "frozen", sha="a" * 40, actor="a1")

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
        self.assertIn("- Auteur : a1 (harnais deepseek)", item["body"])
        self.assertIn("codex", item["body"])   # famille opposée à deepseek
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

    def test_deux_ouvertures_concurrentes_ne_donnent_qu_un_lot(self):
        """Revue #544 r2 : le verrou de la ligne marquée sérialise les appels ;
        le perdant relit la référence, voit le lot du gagnant et rend son
        identifiant sans rien écrire."""
        import threading
        ref = review_lot.ref_of(self.lot)
        other = self.connect()
        opened = other.transaction()
        tx = opened.__enter__()
        outcome: dict = {}
        thread = threading.Thread(target=lambda: outcome.update(
            out=review_lot.open_after_merge(
                self.db, lot=self.lot, klass={"class": "light", "why": "policy"},
                merged_sha="f" * 40)))
        try:
            tx.query("SELECT id FROM work_items WHERE id = %s FOR NO KEY UPDATE", (self.lot,))
            thread.start()
            thread.join(1.5)
            self.assertTrue(thread.is_alive(), "l'appel n'a pas attendu le verrou")
            winner = tx.query(
                "INSERT INTO work_items (type, source, app, title, body, issue_ref, state)"
                " VALUES ('evolution', %s, 'ameesh', 'REVUE', '', %s, 'intake')"
                " RETURNING id", (ref, ref))[0]["id"]
        finally:
            opened.__exit__(None, None, None)
            thread.join(30)
            other.close()
        self.assertFalse(thread.is_alive())
        out = outcome["out"]
        self.assertFalse(out["created"], out)
        self.assertEqual(out["id"], winner)
        self.assertEqual([i["id"] for i in storage.of(self.db).work.by_issue_ref(ref)],
                         [winner])

    def test_un_lot_inconnu_est_rendu_sans_erreur(self):
        out = review_lot.open_after_merge(self.db, lot=99999,
                                          klass={"class": "light"}, merged_sha="c" * 40)
        self.assertFalse(out["created"])
        self.assertIn("introuvable", out["why"])


if __name__ == "__main__":
    unittest.main()
