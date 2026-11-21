# SPDX-License-Identifier: AGPL-3.0-only
"""#7988 — un même test rouge en prévalidation invitée sur 2 lots distincts.

Le 20/11, test_l232d rouge dans l'invité et vert en local a retenu 5 gels de
3 lots (#7970) : chaque gel était jugé seul. Ces témoins vérifient que le même
test déjà rouge sur un autre lot est classé environnement (non concluant,
§3), qu'un rouge propre au lot reste rouge, que les porteurs de paquet et le
lot lui-même ne comptent pas, la lecture bornée du stockage, et le lot de
cause ouvert une fois par module.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from ameesh import prevalidation as pv
from ameesh import prevalidation_calls as calls
from ameesh import storage

from .support import PgTestCase

A = "a" * 40
REAL = "tests.test_real.C.test_x"
OTHER = "tests.test_autre.C.test_x"


def selection() -> pv.Selection:
    return pv.Selection(
        head=A, modules=("test_real", "test_autre"),
        parts=({"driver": "psycopg", "part": 1, "of": 2, "modules": ("test_real",)},
               {"driver": "psycopg", "part": 2, "of": 2, "modules": ("test_autre",)}))


def flow(red=("test_real",)) -> dict:
    lignes: list[str] = []
    for part, module in (("psycopg:1", "test_real"), ("psycopg:2", "test_autre")):
        statut = "echec" if module in red else "succes"
        lignes.append(pv.PART_MARK + part)
        lignes.append(json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "test",
                                  "part": part, "module": module,
                                  "test": "tests.%s.C.test_x" % module,
                                  "statut": statut}))
        lignes.append(json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "fin",
                                  "part": part, "statut": statut, "duree_s": 1.0}))
    return pv.parse_flow("\n".join(lignes))


def proof(recurrent=None, red=("test_real",)) -> dict:
    return selection().proof(lot=7988, host="ameesh-agents-3",
                             run={"outcome": "run-red", "exit": 1, "seconds": 1.0},
                             flow=flow(red), recurrent=recurrent)


class ProofTest(unittest.TestCase):
    def test_seul_lot_rouge_reste_rouge(self):
        report = proof()
        self.assertIn("run-red", report["causes"])
        self.assertNotIn("environment_recurrent", report)
        self.assertEqual(calls.report_state(report), "red")

    def test_meme_test_rouge_sur_un_autre_lot_est_environnement(self):
        report = proof({REAL: [7970]})
        self.assertEqual(report["causes"], ["run-incomplete"])
        self.assertEqual(calls.report_state(report), "unavailable")
        self.assertEqual(report["environment_recurrent"],
                         [{"test": REAL, "lots": [7970]}])
        # le test reste nommé en échec : un troisième lot le reconnaît
        self.assertEqual(report["failed"], [REAL])
        self.assertIn("#7970", " ".join(report["detail"]))

    def test_le_lot_lui_meme_ne_compte_pas(self):
        report = proof({REAL: [7988]})
        self.assertIn("run-red", report["causes"])
        self.assertNotIn("environment_recurrent", report)

    def test_un_rouge_propre_au_lot_reste_rouge_a_cote(self):
        report = proof({REAL: [7970]}, red=("test_real", "test_autre"))
        self.assertIn("run-red", report["causes"])
        self.assertEqual(calls.report_state(report), "red")
        self.assertEqual([e["test"] for e in report["environment_recurrent"]], [REAL])

    def test_part_rouge_anonyme_reste_rouge(self):
        lignes = [pv.PART_MARK + "psycopg:1",
                  json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "test",
                              "part": "psycopg:1", "module": "test_real",
                              "test": REAL, "statut": "echec"}),
                  json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "fin",
                              "part": "psycopg:1", "statut": "echec", "duree_s": 1.0}),
                  pv.PART_MARK + "psycopg:2",
                  json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "test",
                              "part": "psycopg:2", "module": "test_autre",
                              "test": OTHER, "statut": "succes"}),
                  json.dumps({"v": 1, "schema": pv.FLOW_SCHEMA, "type": "fin",
                              "part": "psycopg:2", "statut": "echec", "duree_s": 1.0})]
        report = selection().proof(
            lot=7988, host="vm1", run={"outcome": "run-red", "exit": 1, "seconds": 1.0},
            flow=pv.parse_flow("\n".join(lignes)), recurrent={REAL: [7970]})
        self.assertIn("run-red", report["causes"])


class RecurrentFailuresTest(unittest.TestCase):
    def test_lots_distincts_hors_porteurs_et_lot_courant(self):
        rows = [{"work_item_id": 7970, "report": {"failed": [REAL]}},
                {"work_item_id": 7970, "report": json.dumps({"failed": [REAL]})},
                {"work_item_id": 7699, "report": {"failed": [REAL, OTHER]}},
                {"work_item_id": 7988, "report": {"failed": [OTHER]}},
                {"work_item_id": 8000, "report": {"failed": [OTHER]}},
                {"work_item_id": 0, "report": {"failed": [OTHER]}},
                {"work_item_id": 9, "report": "illisible"}]
        seen = pv.recurrent_failures(rows, 7988, ignored=(8000,))
        self.assertEqual(seen, {REAL: [7699, 7970], OTHER: [7699]})


def lot(app="ameesh", branch="lot/x", issue_ref=None) -> dict:
    return {"app": app, "branch": branch, "issue_ref": issue_ref}


#: projets configurés : `ameesh` → org/core, `nexlink` → org/nex
SETTINGS = mock.Mock()
SETTINGS.repo_for.side_effect = {"ameesh": "org/core", "nexlink": "org/nex"}.get


class SeenElsewhereTest(unittest.TestCase):
    def seen(self, items: dict, rows, current=7988, settings=SETTINGS):
        store = mock.Mock()
        store.prevalidation.failures_since.return_value = rows
        with mock.patch.object(storage, "of", return_value=store), \
                mock.patch.object(calls.work, "get",
                                  side_effect=lambda db, n: items.get(n)), \
                mock.patch("ameesh.plan_github.settings_of", return_value=settings):
            return calls.seen_elsewhere(object(), current, cfg=object()), store

    @staticmethod
    def red(*lots):
        return [{"work_item_id": n, "report": {"failed": [REAL]}} for n in lots]

    def test_lecture_impossible_juge_le_lot_seul(self):
        warned = []
        with mock.patch.object(storage, "of", side_effect=RuntimeError("base")), \
                mock.patch.object(calls.work, "get", return_value=lot()), \
                mock.patch("ameesh.plan_github.settings_of", return_value=SETTINGS):
            self.assertEqual(calls.seen_elsewhere(object(), 7988, cfg=object(),
                                                  warn=warned.append), {})
        self.assertTrue(warned)

    def test_sans_base_rien(self):
        self.assertEqual(calls.seen_elsewhere(None, 7988), {})

    def test_meme_projet_compte(self):
        """Témoin positif (B cx2/dev3) : deux lots du même projet, sans ticket,
        tous deux dans le dépôt configuré."""
        seen, store = self.seen({7988: lot(), 7970: lot()}, self.red(7970))
        self.assertEqual(seen, {REAL: [7970]})
        store.prevalidation.failures_since.assert_called_once_with(
            7988, pv.RECURRENT_WINDOW_S)
        self.assertEqual(calls.report_state(proof(seen)), "unavailable")

    def test_ticket_et_depot_configure_identiques_comptent(self):
        """Positif : un lot sans ticket (dépôt configuré org/core) et un lot
        au ticket org/core se rapprochent."""
        seen, _ = self.seen({7988: lot(), 7970: lot(issue_ref="org/core#5")},
                            self.red(7970))
        self.assertEqual(seen, {REAL: [7970]})

    def test_autre_projet_au_test_homonyme_ne_compte_pas(self):
        """Témoin négatif (B cx2/dev3) : la table est partagée par les projets ;
        le même nom de test rouge dans un autre projet laisse le rouge."""
        seen, _ = self.seen({7988: lot(), 9001: lot(app="nexlink")}, self.red(9001))
        self.assertEqual(seen, {})
        self.assertEqual(calls.report_state(proof(seen)), "red")

    def test_sans_ticket_depot_configure_contre_ticket_autre_depot(self):
        """Négatif (B cx2 #44939) : le lot sans ticket est dans org/core
        (dépôt configuré), pas « tout dépôt » ; un ticket org/ui ne compte pas,
        dans les deux sens."""
        seen, _ = self.seen({7988: lot(), 7971: lot(issue_ref="org/ui#12")},
                            self.red(7971))
        self.assertEqual(seen, {})
        seen, _ = self.seen({7988: lot(issue_ref="org/ui#12"), 7972: lot()},
                            self.red(7972))
        self.assertEqual(seen, {})

    def test_depot_non_prouve_ne_compte_pas(self):
        """Sans réglage pour le projet, un lot sans ticket n'a pas de dépôt
        prouvé : il ne compte pas, et un lot courant ainsi est jugé seul."""
        empty = mock.Mock()
        empty.repo_for.return_value = None
        seen, _ = self.seen({7988: lot(issue_ref="org/core#1"), 7970: lot()},
                            self.red(7970), settings=empty)
        self.assertEqual(seen, {})
        seen, store = self.seen({7988: lot(), 7970: lot(issue_ref="org/core#5")},
                                self.red(7970), settings=empty)
        self.assertEqual(seen, {})
        store.prevalidation.failures_since.assert_not_called()

    def test_lot_illisible_ou_sans_projet_ne_compte_pas(self):
        seen, _ = self.seen({7988: lot(), 7970: lot(app="")}, self.red(7970, 7971))
        self.assertEqual(seen, {})

    def test_porteur_de_paquet_ecarte(self):
        seen, _ = self.seen(
            {7988: lot(), 7970: lot(),
             8000: lot(branch="integration/paquet-h-20261120-13")},
            [{"work_item_id": 7970, "report": {"failed": [REAL]}},
             {"work_item_id": 8000, "report": {"failed": [OTHER]}}])
        self.assertEqual(seen, {REAL: [7970]})

    def test_porteur_courant_juge_seul(self):
        seen, store = self.seen(
            {8000: lot(branch="integration/paquet-h-20261120-13")}, [], current=8000)
        self.assertEqual(seen, {})
        store.prevalidation.failures_since.assert_not_called()


class CauseLotTest(unittest.TestCase):
    def test_un_lot_par_module_avec_source_stable(self):
        report = {"lot": 7988, "sha": A, "environment_recurrent": [
            {"test": REAL, "lots": [7970]},
            {"test": "tests.test_real.D.test_y", "lots": [7699, 7970]},
            {"test": OTHER, "lots": [7699]}]}
        with mock.patch.object(calls.work, "add_or_note",
                               side_effect=lambda db, **kw: kw) as add:
            made = calls.cause_lots(None, object(), report, project="ameesh")
        self.assertEqual(add.call_count, 2)
        sources = [m["source"] for m in made]
        self.assertEqual(sources, ["prevalidation-environment:ameesh:test_autre",
                                   "prevalidation-environment:ameesh:test_real"])
        real = made[1]
        self.assertIn("3 lots distincts", real["title"])
        self.assertIn("#7699, #7970, #7988", real["title"])
        self.assertEqual(real["footprint"], ["tests/test_real.py"])
        self.assertEqual(real["type"], "bug")

    def test_echec_de_creation_signale_sans_lever(self):
        warned = []
        report = {"lot": 7988, "sha": A,
                  "environment_recurrent": [{"test": REAL, "lots": [7970]}]}
        with mock.patch.object(calls.work, "add_or_note", side_effect=RuntimeError("x")):
            self.assertEqual(calls.cause_lots(None, object(), report, project="ameesh",
                                              warn=warned.append), [])
        self.assertTrue(warned)


class StoreTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM prevalidation_reports")
        self.store = storage.of(self.db).prevalidation

    def record(self, lot, sha, *, ok=False, failed=()):
        self.store.record({"schema": pv.REPORT_SCHEMA, "lot": lot, "sha": sha,
                           "outcome": "green" if ok else "run-red", "ok": ok,
                           "causes": [] if ok else ["run-red"],
                           "failed": list(failed)})

    def test_rouges_recents_des_autres_lots(self):
        self.record(7970, "1" * 40, failed=[REAL])
        self.record(7988, "2" * 40, failed=[REAL])
        self.record(7699, "3" * 40, ok=True)
        self.record(7700, "4" * 40)
        self.record(7701, "5" * 40, failed=[OTHER])
        self.db.execute("UPDATE prevalidation_reports SET recorded_at ="
                        " now() - interval '3 days' WHERE work_item_id = 7701")
        rows = self.store.failures_since(7988, pv.RECURRENT_WINDOW_S)
        self.assertEqual([r["work_item_id"] for r in rows], [7970])
        self.assertEqual(pv.recurrent_failures(rows, 7988), {REAL: [7970]})


if __name__ == "__main__":
    unittest.main()
